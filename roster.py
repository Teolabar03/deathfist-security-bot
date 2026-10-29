import asyncio
import hashlib
import json
import os

import httpx

import database
from constants import DEFAULT_CHARACTER_LABEL

# Versione esplicita: senza, Discord risponde col vecchio formato e retry_after è in millisecondi
DISCORD_API = "https://discord.com/api/v10"

DEFAULT_COLOR = 0xF1C40F  # Giallo Paul Phoenix
PRO_COLOR = 0x3B82F6  # Blu, per il ruolo Pro

REQUEST_PACING_SECONDS = 0.3
MAX_RATE_LIMIT_RETRIES = 5

_lock = asyncio.Lock()
_roles_lock = asyncio.Lock()


def _display_name(member: dict) -> str:
    return member["alias"] if member.get("alias") else member["discord_name"]


def _build_member_embed(member: dict) -> dict:
    chars = ", ".join(member["characters"]) if member["characters"] else DEFAULT_CHARACTER_LABEL
    color = PRO_COLOR if member.get("is_pro") else DEFAULT_COLOR
    return {
        "embeds": [{
            "title": _display_name(member),
            "description": f"**Main Character(s):** {chars}",
            "color": color,
        }]
    }


def _content_hash(member: dict) -> str:
    payload = json.dumps({
        "name": _display_name(member),
        "characters": member["characters"],
        "is_pro": bool(member.get("is_pro")),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


async def _request(client: httpx.AsyncClient, method: str, url: str, headers: dict, json_body: dict = None, params: dict = None):
    """Esegue una richiesta rispettando il rate limit di Discord (retry su 429 con backoff)."""
    for attempt in range(MAX_RATE_LIMIT_RETRIES):
        resp = await client.request(method, url, json=json_body, params=params, headers=headers)
        if resp.status_code == 429:
            try:
                retry_after = float(resp.json().get("retry_after", 1))
            except Exception:
                retry_after = 1.0
            await asyncio.sleep(retry_after + 0.25)
            continue
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            # Bucket esaurito (es. 10 ruoli ogni 10 s): si aspetta il reset invece di prendersi un 429
            try:
                await asyncio.sleep(float(resp.headers.get("X-RateLimit-Reset-After", 1)) + 0.25)
            except ValueError:
                await asyncio.sleep(1)
        else:
            await asyncio.sleep(REQUEST_PACING_SECONDS)
        return resp
    return resp


async def _delete_message(client: httpx.AsyncClient, channel_id: str, message_id: str, headers: dict):
    try:
        await _request(client, "DELETE", f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}", headers)
    except Exception:
        pass


async def _get_bot_user_id(client: httpx.AsyncClient, headers: dict) -> str:
    resp = await _request(client, "GET", f"{DISCORD_API}/users/@me", headers)
    resp.raise_for_status()
    return resp.json()["id"]


async def _list_bot_message_ids(client: httpx.AsyncClient, channel_id: str, headers: dict, bot_user_id: str) -> set:
    """Elenca gli ID di tutti i messaggi del bot realmente presenti nel canale (una sola lettura,
    usata sia per rilevare messaggi cancellati manualmente sia per la pulizia in fase di rebuild)."""
    ids = set()
    before = None
    while True:
        params = {"limit": 100}
        if before:
            params["before"] = before
        resp = await _request(client, "GET", f"{DISCORD_API}/channels/{channel_id}/messages", headers, params=params)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for msg in batch:
            if msg.get("author", {}).get("id") == bot_user_id:
                ids.add(msg["id"])
        before = batch[-1]["id"]
        if len(batch) < 100:
            break
    return ids


def _match_character_roles(roles: list, characters: list) -> dict:
    """Nome del personaggio nel catalogo -> ID del ruolo Discord con lo stesso nome (senza maiuscole).
    Se il ruolo porta solo il primo nome (es. 'Anna' per 'Anna Williams') vale anche quello,
    purché nel catalogo non esista un personaggio che si chiama proprio così."""
    by_name = {}
    for r in roles:
        if not r.get("managed"):
            by_name.setdefault(r["name"].strip().lower(), r["id"])
    catalog = {c.lower() for c in characters}
    result = {}
    for c in characters:
        key = c.lower()
        role_id = by_name.get(key)
        if role_id is None and " " in key:
            first = key.split()[0]
            if first not in catalog:
                role_id = by_name.get(first)
        if role_id:
            result[c] = role_id
    return result


async def _list_guild_members(client: httpx.AsyncClient, guild_id: str, headers: dict) -> list:
    members = []
    after = "0"
    while True:
        resp = await _request(client, "GET", f"{DISCORD_API}/guilds/{guild_id}/members", headers,
                              params={"limit": 1000, "after": after})
        resp.raise_for_status()
        batch = resp.json()
        members.extend(batch)
        if len(batch) < 1000:
            return members
        after = batch[-1]["user"]["id"]


async def sync_character_roles(bot_token: str = None) -> dict:
    """Allinea personaggi e ruoli Discord in entrambe le direzioni:
    - legge i ruoli personaggio di ogni membro e li salva (usati nel roster se il backend non ha dati);
    - se il backend assegna a un membro un personaggio di cui non ha il ruolo, glielo aggiunge.
    I ruoli non vengono mai tolti. Restituisce quanti ruoli ha aggiunto e se manca il permesso."""
    bot_token = bot_token or os.getenv("DISCORD_TOKEN")
    headers = {"Authorization": f"Bot {bot_token}"}
    add_headers = {**headers, "X-Audit-Log-Reason": "Roster DeathFist: personaggio assegnato dal pannello"}
    result = {"assigned": 0, "missing_permission": False}

    async with _roles_lock:
        characters = database.get_all_characters()
        known = {m["discord_id"]: m for m in database.get_members_with_characters(include_excluded=True)}

        # Personaggi dai ruoli, uniti su tutti i server in cui il membro compare
        from_roles = {}

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await _request(client, "GET", f"{DISCORD_API}/users/@me/guilds", headers)
            resp.raise_for_status()
            for guild in resp.json():
                guild_id = guild["id"]
                resp = await _request(client, "GET", f"{DISCORD_API}/guilds/{guild_id}/roles", headers)
                resp.raise_for_status()
                role_ids = _match_character_roles(resp.json(), characters)
                if not role_ids:
                    continue

                denied = False
                for gm in await _list_guild_members(client, guild_id, headers):
                    discord_id = gm["user"]["id"]
                    if discord_id not in known or gm["user"].get("bot"):
                        continue

                    has_roles = set(gm["roles"])
                    from_roles.setdefault(discord_id, set()).update(
                        c for c, rid in role_ids.items() if rid in has_roles
                    )

                    if denied:
                        continue
                    # Riletto qui: dal pannello potrebbe essere cambiato mentre la sync era in corso
                    for character in database.get_character_assignments(discord_id):
                        role_id = role_ids.get(character)
                        if role_id is None or role_id in has_roles:
                            continue
                        resp = await _request(
                            client, "PUT",
                            f"{DISCORD_API}/guilds/{guild_id}/members/{discord_id}/roles/{role_id}",
                            add_headers,
                        )
                        if resp.status_code == 403:
                            # Manca "Gestisci ruoli" o il ruolo del bot sta sotto quello del personaggio
                            print(f"[ROLE SYNC] Permesso negato nell'assegnare i ruoli su {guild['name']}", flush=True)
                            result["missing_permission"] = True
                            denied = True
                            break
                        resp.raise_for_status()
                        result["assigned"] += 1

        for discord_id, member in known.items():
            names = sorted(from_roles.get(discord_id, ()))
            if names != member["role_characters"]:
                database.set_role_characters(discord_id, names)

    if result["assigned"]:
        print(f"[ROLE SYNC] Assegnati {result['assigned']} ruoli personaggio", flush=True)
    return result


async def sync_all_member_messages(channel_id: str = None, bot_token: str = None):
    """Un messaggio Discord per membro (ordinati per alias, o nome se assenza di alias).
    Discord non permette di riordinare messaggi già inviati: quindi ogni volta che serve un NUOVO invio
    (membro nuovo/rimosso, ordine cambiato, o un messaggio esistente è sparito) si ricostruisce l'intero
    canale da zero, in ordine. Se invece nulla richiede un nuovo invio, i messaggi il cui contenuto è
    cambiato vengono solo aggiornati in place (PATCH), senza toccare l'ordine."""
    channel_id = channel_id or os.getenv("ROSTER_CHANNEL_ID")
    bot_token = bot_token or os.getenv("DISCORD_TOKEN")
    headers = {"Authorization": f"Bot {bot_token}"}

    async with _lock:
        members = database.get_members_with_characters(include_bots=False, include_excluded=False)
        current_order = [m["discord_id"] for m in members]

        stored_order_raw = database.get_bot_state("roster_order")
        stored_order = json.loads(stored_order_raw) if stored_order_raw else []

        existing_msgs = database.get_roster_messages()

        async with httpx.AsyncClient(timeout=30) as client:
            bot_user_id = await _get_bot_user_id(client, headers)

            # Pulizia una tantum dell'eventuale vecchio messaggio unico (formato precedente)
            legacy_raw = database.get_bot_state("roster_message_ids")
            if legacy_raw:
                for mid in json.loads(legacy_raw):
                    await _delete_message(client, channel_id, mid, headers)
                database.set_bot_state("roster_message_ids", "")

            # Lettura reale del canale: rileva sia gli orfani sia i messaggi cancellati manualmente.
            # Viene eseguita ad ogni sync (eventi in tempo reale + task periodico), non solo quando
            # cambia il contenuto di un membro.
            channel_message_ids = await _list_bot_message_ids(client, channel_id, headers, bot_user_id)

            any_message_missing = any(
                info["message_id"] not in channel_message_ids for info in existing_msgs.values()
            )
            needs_rebuild = (current_order != stored_order) or any_message_missing

            if needs_rebuild:
                # Serve almeno un nuovo invio: si ricostruisce TUTTO il canale per garantire l'ordinamento.
                # Si cancellano tutti i messaggi del bot realmente presenti (non solo quelli tracciati) per
                # evitare che eventuali orfani da crash/errori precedenti restino accumulati.
                for mid in channel_message_ids:
                    await _delete_message(client, channel_id, mid, headers)
                database.clear_roster_messages()

                for m in members:
                    payload = _build_member_embed(m)
                    resp = await _request(
                        client, "POST", f"{DISCORD_API}/channels/{channel_id}/messages", headers, payload
                    )
                    resp.raise_for_status()
                    database.set_roster_message(m["discord_id"], str(resp.json()["id"]), _content_hash(m))

                database.set_bot_state("roster_order", json.dumps(current_order))
            else:
                # Nessun nuovo invio necessario: aggiorna in place solo ciò il cui contenuto è cambiato
                for m in members:
                    h = _content_hash(m)
                    info = existing_msgs[m["discord_id"]]
                    if info["content_hash"] == h:
                        continue

                    payload = _build_member_embed(m)
                    resp = await _request(
                        client, "PATCH",
                        f"{DISCORD_API}/channels/{channel_id}/messages/{info['message_id']}",
                        headers, payload,
                    )
                    resp.raise_for_status()
                    database.set_roster_message(m["discord_id"], info["message_id"], h)
