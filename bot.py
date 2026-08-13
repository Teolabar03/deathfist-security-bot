import discord
from discord.ext import commands, tasks
from discord import app_commands
from collections import deque
import datetime
import os
from dotenv import load_dotenv

import database
import roster
from challonge_client import challonge
from constants import PRO_ROLE_NAME

load_dotenv()

def _require_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Variabile d'ambiente obbligatoria mancante: {name}. Copia .env.example in .env e compilala.")
    return value

# --- CONFIGURAZIONE (nessun default: obbligatorie in .env) ---
TOKEN = _require_env("DISCORD_TOKEN")
LOG_CHANNEL_ID = int(_require_env("LOG_CHANNEL_ID"))
ROSTER_CHANNEL_ID = int(_require_env("ROSTER_CHANNEL_ID"))
WEB_BASE_URL = os.getenv("WEB_BASE_URL", "http://localhost:8000")
SPAM_THRESHOLD = 3
TIME_WINDOW = 10

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class DeathFistBot(commands.Bot):
    async def setup_hook(self):
        database.init_db()


bot = DeathFistBot(command_prefix="!", intents=intents)

# Dizionario per tracciare i messaggi: {user_id: deque([(testo, timestamp), ...])}
user_logs = {}


@bot.event
async def on_ready():
    total = 0
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            total += len(synced)
            print(f"[SYNC] {len(synced)} comandi -> {guild.name} ({guild.id})", flush=True)
        except Exception as e:
            print(f"[SYNC ERROR] {guild.name}: {e}", flush=True)
    print(f"Bot avviato come {bot.user} | {total} comandi sincronizzati su {len(bot.guilds)} server", flush=True)

    await full_roster_sync()

    if not periodic_roster_sync.is_running():
        periodic_roster_sync.start()


@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: Exception):
    print(f"[SLASH ERROR] /{interaction.command.name if interaction.command else '?'}: {error}", flush=True)
    if not interaction.response.is_done():
        await interaction.response.send_message("❌ Errore interno. Riprova.", ephemeral=True)


# =============================================================
# --- ROSTER MEMBRI ---
# =============================================================

def _has_pro_role(member: discord.Member) -> bool:
    return any(r.name.lower() == PRO_ROLE_NAME.lower() for r in member.roles)


async def full_roster_sync():
    """Reimporta tutti i membri dai server del bot (aggiunge i mancanti, rimuove chi non c'è più),
    poi risincronizza i messaggi roster (uno per membro; li ricrea se sono stati cancellati)."""
    all_ids = []
    for guild in bot.guilds:
        try:
            async for member in guild.fetch_members(limit=None):
                database.upsert_member(str(member.id), str(member), is_bot=member.bot, is_pro=_has_pro_role(member))
                all_ids.append(str(member.id))
        except Exception as e:
            print(f"[ROSTER SYNC ERROR] {guild.name}: {e}", flush=True)

    if all_ids:
        database.prune_members_not_in(all_ids)

    try:
        await roster.sync_all_member_messages()
    except Exception as e:
        print(f"[ROSTER ERROR] {e}", flush=True)


@tasks.loop(minutes=10)
async def periodic_roster_sync():
    await full_roster_sync()


@bot.event
async def on_member_join(member: discord.Member):
    database.upsert_member(str(member.id), str(member), is_bot=member.bot, is_pro=_has_pro_role(member))
    try:
        await roster.sync_all_member_messages()
    except Exception as e:
        print(f"[ROSTER ERROR] {e}", flush=True)


@bot.event
async def on_member_remove(member: discord.Member):
    database.remove_member(str(member.id))
    try:
        await roster.sync_all_member_messages()
    except Exception as e:
        print(f"[ROSTER ERROR] {e}", flush=True)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    was_pro = _has_pro_role(before)
    is_pro = _has_pro_role(after)
    if was_pro == is_pro and str(before) == str(after):
        return
    database.upsert_member(str(after.id), str(after), is_bot=after.bot, is_pro=is_pro)
    try:
        await roster.sync_all_member_messages()
    except Exception as e:
        print(f"[ROSTER ERROR] {e}", flush=True)


@bot.event
async def on_message(message):
    # Ignora i messaggi dei bot (incluso se stesso)
    if message.author.bot or not message.guild:
        return

    user_id = message.author.id
    current_time = datetime.datetime.now(datetime.timezone.utc)
    content = message.content.strip().lower()

    # Salta messaggi vuoti (es. solo immagini)
    if not content:
        return

    if user_id not in user_logs:
        user_logs[user_id] = deque()

    # Aggiungi il messaggio corrente alla cronologia dell'utente
    user_logs[user_id].append((content, current_time))

    # Pulisci i messaggi più vecchi della finestra temporale stabilita
    while user_logs[user_id] and (current_time - user_logs[user_id][0][1]).total_seconds() > TIME_WINDOW:
        user_logs[user_id].popleft()

    # Conta quanti messaggi identici ci sono nella finestra temporale
    same_messages = [msg for msg, ts in user_logs[user_id] if msg == content]

    if len(same_messages) >= SPAM_THRESHOLD:
        # Pulizia immediata del log utente per evitare trigger multipli consecutivi
        user_logs[user_id].clear()
        await sanziona_spammer(message, content)

    await bot.process_commands(message)


async def sanziona_spammer(message, testo_incriminato):
    guild = message.guild
    member = message.author

    # 1. Applica il Timeout (Il Death Fist colpisce!)
    durata_timeout = datetime.timedelta(minutes=10)
    try:
        await member.timeout(durata_timeout, reason="Rilevato spam. PHOENIX SMASHER!")
        stato_timeout = "Time out avviato (10 min)"
    except discord.Forbidden:
        stato_timeout = "Tentativo fallito! (Owner/Admin - Timeout fallito)"
        print(f"⚠️ [LOG] Rilevato spam da {member.name}, ma ha ruoli superiori o è l'Owner. Procedo con l'invio nel canale di controllo.")

    # 2. Canale di controllo stile Tekken Match Report
    log_channel = guild.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(
            title="🔥 DEATH FIST SECURITY SYSTEM 🔥",
            description=f"**{member.name}** ha eseguito troppe mosse ripetute. Paul Phoenix ha caricato il destro!",
            color=discord.Color.from_rgb(241, 196, 15),  # Giallo Paul Phoenix
            timestamp=datetime.datetime.now()
        )
        embed.set_thumbnail(url="https://i.imgur.com/vHdf3iH.png")

        embed.add_field(name="Sfidante KO", value=f"{member.mention} ({member.id})", inline=True)
        embed.add_field(name="Esito dell'Impatto", value=stato_timeout, inline=True)
        embed.add_field(name="Spam (Mosse ripetute)", value=f"```{testo_incriminato}```", inline=False)
        embed.set_footer(text="EAAGH! No-one can catch my punch!")

        view = SecurityActionView(member_id=member.id)
        await log_channel.send(embed=embed, view=view)


# --- INTERFACCIA PULSANTI STILE TEKKEN ---
class SecurityActionView(discord.ui.View):
    def __init__(self, member_id: int):
        super().__init__(timeout=None)  # Mantiene i pulsanti attivi a tempo indeterminato
        self.member_id = member_id

    async def get_member(self, interaction: discord.Interaction):
        """Helper per recuperare il membro anche se ha lasciato o è successo qualcosa"""
        member = interaction.guild.get_member(self.member_id)
        if not member:
            await interaction.response.send_message("Lo sfidante è già fuggito dal ring.", ephemeral=True)
        return member

    @discord.ui.button(label="Rematch (Annulla KO / Smuta)", style=discord.ButtonStyle.success, emoji="🔄")
    async def unmute_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = await self.get_member(interaction)
        if member:
            await member.timeout(None, reason=f"Annullato da {interaction.user}")
            await interaction.response.send_message(f"🟢 {interaction.user.mention} ha concesso il Rematch a {member.mention}! (Smutato)", ephemeral=False)
            self.stop()  # Disabilita questa specifica view dopo l'azione

    @discord.ui.button(label="PERFECT! (Espelli / Kick)", style=discord.ButtonStyle.primary, emoji="👢")
    async def kick_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = await self.get_member(interaction)
        if member:
            await member.kick(reason=f"Espulso dal ring da {interaction.user}")
            await interaction.response.send_message(f"📣 **YOU WIN!** {interaction.user.mention} ha buttato fuori dal ring {member.name}!", ephemeral=False)
            self.stop()

    @discord.ui.button(label="EAAGH! (Ban Permanente)", style=discord.ButtonStyle.danger, emoji="💥")
    async def ban_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = await self.get_member(interaction)
        if member:
            await interaction.guild.ban(member, reason=f"Distrutto da Paul Phoenix e {interaction.user}", delete_message_days=1)
            await interaction.response.send_message(f"💀 **STRONGEST IN THE UNIVERSE!** {interaction.user.mention} ha disintegrato {member.name} con un Death Fist definitivo! (Bannato)", ephemeral=False)
            self.stop()


# =============================================================
# --- SLASH COMMANDS: TORNEO ---
# =============================================================

@bot.tree.command(name="iscriviti", description="Iscriviti al torneo attivo")
@app_commands.describe(
    personaggio="Il tuo personaggio principale di Tekken 8",
    nome_tekken="Il tuo ID in-game su Tekken 8 (opzionale)",
)
async def iscriviti(
    interaction: discord.Interaction,
    personaggio: str,
    nome_tekken: str = None,
):
    tournament = database.get_open_tournament()
    if not tournament:
        await interaction.response.send_message(
            "❌ Non c'è nessun torneo aperto alle iscrizioni al momento.", ephemeral=True
        )
        return

    existing = database.get_participant(str(interaction.user.id))
    if existing and existing["tournament_id"] == tournament["id"]:
        await interaction.response.send_message(
            "⚠️ Sei già iscritto a questo torneo!", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        participant = await challonge.add_participant(
            tournament["id"],
            interaction.user.display_name,
            misc=personaggio,
        )
        database.add_participant(
            discord_id=str(interaction.user.id),
            discord_name=str(interaction.user),
            tekken_id=nome_tekken,
            main_character=personaggio,
            challonge_participant_id=str(participant["id"]),
            tournament_id=tournament["id"],
        )
        await interaction.followup.send(
            f"✅ **Iscrizione completata!**\n"
            f"**Torneo:** {tournament['name']}\n"
            f"**Personaggio:** {personaggio}\n"
            f"**Tekken ID:** {nome_tekken or 'Non specificato'}",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Errore durante l'iscrizione: {e}", ephemeral=True)


@iscriviti.autocomplete("personaggio")
async def character_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=char, value=char)
        for char in database.get_all_characters()
        if current.lower() in char.lower()
    ][:25]


@bot.tree.command(name="report", description="Riporta il risultato del tuo match")
@app_commands.describe(
    tuo_score="I tuoi punti vinti",
    avversario_score="I punti vinti dall'avversario",
)
async def report(interaction: discord.Interaction, tuo_score: int, avversario_score: int):
    tournament = database.get_active_tournament()
    if not tournament:
        await interaction.response.send_message(
            "❌ Non c'è nessun torneo attivo al momento.", ephemeral=True
        )
        return

    participant = database.get_participant(str(interaction.user.id))
    if not participant or participant["tournament_id"] != tournament["id"]:
        await interaction.response.send_message(
            "❌ Non sei registrato nel torneo attivo.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        matches = await challonge.get_matches(tournament["id"], state="open")
        challonge_pid = participant["challonge_participant_id"]

        active_match = None
        for m in matches:
            if str(m["player1_id"]) == challonge_pid or str(m["player2_id"]) == challonge_pid:
                active_match = m
                break

        if not active_match:
            await interaction.followup.send(
                "⚠️ Non hai un match aperto al momento. Aspetta che l'admin avvii il torneo.",
                ephemeral=True,
            )
            return

        is_player1 = str(active_match["player1_id"]) == challonge_pid

        if is_player1:
            scores_csv = f"{tuo_score}-{avversario_score}"
            winner_id = active_match["player1_id"] if tuo_score > avversario_score else active_match["player2_id"]
        else:
            scores_csv = f"{avversario_score}-{tuo_score}"
            winner_id = active_match["player2_id"] if tuo_score > avversario_score else active_match["player1_id"]

        if tuo_score == avversario_score:
            await interaction.followup.send("❌ Il punteggio non può essere in pareggio.", ephemeral=True)
            return

        await challonge.report_match(
            tournament["id"],
            str(active_match["id"]),
            str(winner_id),
            scores_csv,
        )

        esito = "✅ **HAI VINTO!**" if tuo_score > avversario_score else "💀 **HAI PERSO.**"
        await interaction.followup.send(
            f"{esito}\n**Risultato:** {tuo_score} - {avversario_score}\nMatch registrato correttamente.",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Errore: {e}", ephemeral=True)


@bot.tree.command(name="tabellone", description="Visualizza il tabellone del torneo attivo")
async def tabellone(interaction: discord.Interaction):
    tournament = database.get_active_tournament() or database.get_open_tournament()
    if not tournament:
        await interaction.response.send_message(
            "❌ Nessun torneo attivo o in registrazione al momento.", ephemeral=True
        )
        return

    url = f"{WEB_BASE_URL}/torneo/{tournament['id']}"
    embed = discord.Embed(
        title=f"🏆 {tournament['name']}",
        description=f"**Stato:** {tournament['status']}\n**Formato:** FT{tournament['rules_ft']}\n\n[📊 Apri il tabellone]({url})",
        color=discord.Color.from_rgb(241, 196, 15),
    )
    embed.set_footer(text="DEATH FIST TOURNAMENT SYSTEM")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="Mostra tutti i comandi del Death Fist Tournament System")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🔥 DEATH FIST TOURNAMENT SYSTEM — Comandi",
        color=discord.Color.from_rgb(241, 196, 15),
    )
    embed.set_thumbnail(url="https://i.imgur.com/vHdf3iH.png")

    embed.add_field(
        name="/iscriviti `personaggio` `[nome_tekken]`",
        value="Iscriviti al torneo attualmente aperto alle registrazioni.\n"
              "Scegli il tuo personaggio dal menu. Il Tekken ID è opzionale.",
        inline=False,
    )
    embed.add_field(
        name="/report `tuo_score` `avversario_score`",
        value="Riporta il risultato del tuo match in corso.\n"
              "Esempio: `/report 3 1` se hai vinto 3 a 1.",
        inline=False,
    )
    embed.add_field(
        name="/tabellone",
        value="Mostra il link al bracket pubblico del torneo attivo.",
        inline=False,
    )
    embed.add_field(
        name="/help",
        value="Mostra questo messaggio.",
        inline=False,
    )
    embed.add_field(
        name="Sistema Anti-Spam",
        value=f"Inviare **{SPAM_THRESHOLD} messaggi identici** in **{TIME_WINDOW} secondi** "
              f"attiva automaticamente un timeout di 10 minuti. PHOENIX SMASHER!",
        inline=False,
    )
    embed.set_footer(text="EAAGH! No-one can catch my punch!")
    await interaction.response.send_message(embed=embed)


bot.run(TOKEN)
