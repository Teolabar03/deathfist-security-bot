import datetime
import html
import os
import secrets
import urllib.parse
import httpx
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv

import database
import roster
from challonge_client import challonge
from constants import DEFAULT_CHARACTER_LABEL

load_dotenv()

def _require_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Variabile d'ambiente obbligatoria mancante: {name}. Copia .env.example in .env e compilala.")
    return value

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:8000/callback")
SECRET_KEY = _require_env("SECRET_KEY")
ADMIN_IDS_RAW = os.getenv("ADMIN_DISCORD_IDS", "")
ADMIN_IDS = set(uid.strip() for uid in ADMIN_IDS_RAW.split(",") if uid.strip())
APP_PREFIX = os.getenv("APP_PREFIX", "").rstrip("/")

if not ADMIN_IDS:
    # Senza lista admin chiunque abbia un account Discord entrerebbe nel pannello.
    raise RuntimeError("ADMIN_DISCORD_IDS è vuota: indica almeno un ID Discord admin nel .env.")

app = FastAPI(title="Death Fist Tournament Admin")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=86400,
    same_site="lax",
    https_only=DISCORD_REDIRECT_URI.startswith("https://"),
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals["prefix"] = APP_PREFIX

database.init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_current_user(request: Request):
    return request.session.get("user")


def require_admin(request: Request):
    user = get_current_user(request)
    if not user:
        return None
    if user["id"] not in ADMIN_IDS:
        return None
    return user


def _flash(request: Request, msg: str, kind: str = "ok"):
    request.session[f"flash_{kind}"] = msg


def _pop_flash(request: Request):
    ok = request.session.pop("flash_ok", None)
    err = request.session.pop("flash_error", None)
    return {"flash_ok": ok, "flash_error": err}


def _redirect(path: str, status_code: int = 302):
    return RedirectResponse(f"{APP_PREFIX}{path}", status_code=status_code)


def _player_identity(chal_participant, local_map):
    """Identità stabile di un giocatore per storico/Elo: l'ID Discord quando il
    partecipante è registrato via bot, altrimenti il nome Challonge normalizzato."""
    local = local_map.get(str(chal_participant["id"]))
    if local:
        return local["discord_id"], local["discord_name"], local["main_character"] or None
    name = (chal_participant["name"] or "").strip()
    return f"chal:{name.lower()}", name, chal_participant.get("misc") or None


async def _archive_tournament(tournament):
    """Alla chiusura di un torneo: finalizza su Challonge, poi salva nello storico
    locale i piazzamenti finali e tutti i match completati (base del calcolo Elo)."""
    tid = tournament["id"]

    try:
        await challonge.finalize_tournament(tid)
    except Exception:
        # Già finalizzato, o non finalizzabile (es. match mancanti): si archivia comunque
        pass

    chal_participants = await challonge.list_participants(tid)
    local_map = {
        p["challonge_participant_id"]: p
        for p in database.get_participants_by_tournament(tid)
        if p["challonge_participant_id"]
    }

    database.archive_tournament_reset(
        tid, tournament["name"], datetime.datetime.now(datetime.timezone.utc).isoformat()
    )

    identity_by_pid = {}
    for cp in chal_participants:
        key, name, character = _player_identity(cp, local_map)
        identity_by_pid[str(cp["id"])] = (key, name)
        database.add_placement(tid, key, name, cp.get("final_rank"), character)

    matches = await challonge.get_matches(tid)
    matches.sort(key=lambda m: (m.get("suggested_play_order") or 0))
    for m in matches:
        if m["state"] != "complete" or not m.get("winner_id"):
            continue
        winner_id = str(m["winner_id"])
        p1, p2 = str(m["player1_id"]), str(m["player2_id"])
        loser_id = p2 if winner_id == p1 else p1
        winner = identity_by_pid.get(winner_id)
        loser = identity_by_pid.get(loser_id)
        if not winner or not loser:
            continue
        database.add_match_history(
            tid, m.get("round"), m.get("suggested_play_order"),
            winner[0], winner[1], loser[0], loser[1], m.get("scores_csv"),
        )


ELO_START = 1000.0
ELO_K = 32.0


def _compute_elo(history_matches):
    """Elo classico ricalcolato da zero su tutti i match archiviati, in ordine
    cronologico: il risultato è sempre deterministico e coerente con lo storico."""
    ratings = {}
    stats = {}
    for m in history_matches:
        for key, name in ((m["winner_key"], m["winner_name"]), (m["loser_key"], m["loser_name"])):
            if key not in ratings:
                ratings[key] = ELO_START
                stats[key] = {"name": name, "wins": 0, "losses": 0, "tournaments": set()}
            stats[key]["name"] = name  # tiene il nome più recente

        w, l = m["winner_key"], m["loser_key"]
        expected_w = 1.0 / (1.0 + 10 ** ((ratings[l] - ratings[w]) / 400.0))
        ratings[w] += ELO_K * (1.0 - expected_w)
        ratings[l] -= ELO_K * (1.0 - expected_w)

        stats[w]["wins"] += 1
        stats[l]["losses"] += 1
        stats[w]["tournaments"].add(m["tournament_id"])
        stats[l]["tournaments"].add(m["tournament_id"])

    leaderboard = [
        {
            "player_key": key,
            "name": s["name"],
            "rating": round(ratings[key]),
            "wins": s["wins"],
            "losses": s["losses"],
            "tournaments": len(s["tournaments"]),
        }
        for key, s in stats.items()
    ]
    leaderboard.sort(key=lambda r: (-r["rating"], r["name"].lower()))
    return leaderboard


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

def _auth_error(message: str, status_code: int = 400):
    # Mai rimandare nella pagina testo arrivato dall'esterno senza escape (XSS riflesso).
    return HTMLResponse(f"<p>{html.escape(message)}</p>", status_code=status_code)


@app.get("/login")
async def login(request: Request):
    # "state" lega il ritorno da Discord a questa sessione (protezione CSRF del login OAuth).
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    query = urllib.parse.urlencode({
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
    })
    return RedirectResponse(f"https://discord.com/api/oauth2/authorize?{query}")


@app.get("/callback")
async def callback(request: Request, code: str = None, error: str = None, state: str = None):
    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or not state or not secrets.compare_digest(expected_state, state):
        return _auth_error("Accesso non valido o scaduto: riprova dal pulsante di login.")
    if error or not code:
        return _auth_error("Accesso annullato o non riuscito su Discord.")

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": DISCORD_REDIRECT_URI,
            },
        )
        if token_resp.status_code != 200:
            return _auth_error("Discord non ha confermato l'accesso: riprova.", status_code=502)

        token_data = token_resp.json()
        access_token = token_data["access_token"]

        user_resp = await client.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_resp.status_code != 200:
            return _auth_error("Impossibile leggere il profilo Discord: riprova.", status_code=502)
        user_data = user_resp.json()

    if user_data["id"] not in ADMIN_IDS:
        return _auth_error("Accesso negato: non sei nella lista degli admin.", status_code=403)

    request.session.clear()
    request.session["user"] = {
        "id": user_data["id"],
        "username": user_data.get("global_name") or user_data["username"],
    }
    return _redirect("/admin/dashboard")


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return _redirect("/")


# ---------------------------------------------------------------------------
# Public home
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    tournaments = database.get_all_tournaments()
    return templates.TemplateResponse(request, "public/home.html", {
        "tournaments": tournaments,
    })


# ---------------------------------------------------------------------------
# Public bracket view
# ---------------------------------------------------------------------------

@app.get("/torneo/{tournament_id}", response_class=HTMLResponse)
async def public_bracket(request: Request, tournament_id: str):
    tournament = database.get_tournament(tournament_id)
    if not tournament:
        raise HTTPException(status_code=404, detail="Torneo non trovato")

    db_participants = database.get_participants_by_tournament(tournament_id)
    local_map = {p["challonge_participant_id"]: p for p in db_participants if p["challonge_participant_id"]}

    try:
        challonge_participants = await challonge.list_participants(tournament_id)
    except Exception:
        challonge_participants = []

    # Include anche i partecipanti aggiunti direttamente su Challonge (non registrati via bot)
    display_participants = list(db_participants)
    for p in challonge_participants:
        if str(p["id"]) not in local_map:
            display_participants.append({
                "discord_name": p["name"],
                "tekken_id": None,
                "main_character": p.get("misc"),
            })

    matches_enriched = []
    if tournament["status"] in ("ACTIVE", "FINISHED"):
        try:
            chal_map = {str(p["id"]): p for p in challonge_participants}

            def _player_name(chal_id):
                if not chal_id:
                    return None
                local = local_map.get(str(chal_id))
                if local:
                    name = local["discord_name"]
                    if local["tekken_id"]:
                        name += f" ({local['tekken_id']})"
                    return name
                p = chal_map.get(str(chal_id))
                return p["name"] if p else str(chal_id)

            raw_matches = await challonge.get_matches(tournament_id)
            for m in raw_matches:
                matches_enriched.append({
                    "round": m["round"],
                    "player1_name": _player_name(m["player1_id"]),
                    "player2_name": _player_name(m["player2_id"]),
                    "scores_csv": m.get("scores_csv"),
                    "winner_name": _player_name(m.get("winner_id")),
                    "state": m["state"],
                })
        except Exception:
            pass

    return templates.TemplateResponse(request, "public/bracket.html", {
        "tournament": tournament,
        "participants": display_participants,
        "matches": matches_enriched,
    })


# ---------------------------------------------------------------------------
# Public: storico tornei + classifica Elo
# ---------------------------------------------------------------------------

@app.get("/storico", response_class=HTMLResponse)
async def storico(request: Request):
    history = database.get_tournament_history()
    leaderboard = _compute_elo(database.get_all_history_matches())
    return templates.TemplateResponse(request, "public/storico.html", {
        "history": history,
        "leaderboard": leaderboard,
    })


# ---------------------------------------------------------------------------
# Legal (privacy policy / termini di servizio) — pagine non linkate nel menu,
# raggiungibili solo via URL diretto.
# ---------------------------------------------------------------------------

@app.get("/legal/tekken8-frame-bot/privacy", response_class=HTMLResponse)
async def legal_tekken8_privacy(request: Request):
    return templates.TemplateResponse(request, "legal/tekken8_privacy.html", {})


@app.get("/legal/tekken8-frame-bot/termini", response_class=HTMLResponse)
async def legal_tekken8_terms(request: Request):
    return templates.TemplateResponse(request, "legal/tekken8_terms.html", {})


@app.get("/legal/deathfist/privacy", response_class=HTMLResponse)
async def legal_deathfist_privacy(request: Request):
    return templates.TemplateResponse(request, "legal/deathfist_privacy.html", {})


@app.get("/legal/deathfist/termini", response_class=HTMLResponse)
async def legal_deathfist_terms(request: Request):
    return templates.TemplateResponse(request, "legal/deathfist_terms.html", {})


# ---------------------------------------------------------------------------
# Admin: dashboard
# ---------------------------------------------------------------------------

@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    tournaments = database.get_all_tournaments()
    return templates.TemplateResponse(request, "admin/dashboard.html", {
        "user": user, "tournaments": tournaments, **_pop_flash(request),
    })


@app.get("/admin")
async def admin_root():
    return _redirect("/admin/dashboard")


# ---------------------------------------------------------------------------
# Admin: create tournament
# ---------------------------------------------------------------------------

@app.get("/admin/tournament/new", response_class=HTMLResponse)
async def tournament_new_form(request: Request):
    user = require_admin(request)
    if not user:
        return _redirect("/login")
    return templates.TemplateResponse(request, "admin/tournament_new.html", {
        "user": user, **_pop_flash(request),
    })


@app.post("/admin/tournament/new")
async def tournament_new_submit(
    request: Request,
    name: str = Form(...),
    url_slug: str = Form(...),
    tournament_type: str = Form(...),
    rules_ft: int = Form(...),
):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    try:
        t = await challonge.create_tournament(name, tournament_type, url_slug)
        tournament_id = t.get("url") or str(t["id"])
        database.create_tournament(id=tournament_id, name=name, status="SETUP", rules_ft=rules_ft)
        _flash(request, f"Torneo '{name}' creato con ID: {tournament_id}", "ok")
        return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)
    except Exception as e:
        _flash(request, f"Errore creazione torneo: {e}", "error")
        return _redirect("/admin/tournament/new", status_code=303)


# ---------------------------------------------------------------------------
# Admin: tournament detail
# ---------------------------------------------------------------------------

@app.get("/admin/tournament/{tournament_id}", response_class=HTMLResponse)
async def tournament_detail(request: Request, tournament_id: str):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    tournament = database.get_tournament(tournament_id)
    if not tournament:
        raise HTTPException(status_code=404, detail="Torneo non trovato")

    participants = database.get_participants_by_tournament(tournament_id)

    # Partecipanti presenti su Challonge ma non registrati via bot (aggiunti a mano dall'admin)
    challonge_only = []
    seeding = []
    try:
        chal_participants = await challonge.list_participants(tournament_id)
        local_map = {p["challonge_participant_id"]: p for p in participants if p["challonge_participant_id"]}
        challonge_only = [p for p in chal_participants if str(p["id"]) not in local_map]

        # Lista per l'editor dei seed: tutti i partecipanti Challonge, col nome Discord se collegato
        for cp in chal_participants:
            local = local_map.get(str(cp["id"]))
            seeding.append({
                "id": cp["id"],
                "seed": cp.get("seed"),
                "name": local["discord_name"] if local else cp["name"],
            })
        seeding.sort(key=lambda s: s["seed"] or 0)
    except Exception:
        pass

    return templates.TemplateResponse(request, "admin/tournament.html", {
        "user": user,
        "tournament": tournament,
        "participants": participants,
        "challonge_only": challonge_only,
        "seeding": seeding,
        **_pop_flash(request),
    })


# ---------------------------------------------------------------------------
# Admin: change tournament status
# ---------------------------------------------------------------------------

@app.post("/admin/tournament/{tournament_id}/status")
async def tournament_change_status(
    request: Request,
    tournament_id: str,
    new_status: str = Form(...),
):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    tournament = database.get_tournament(tournament_id)
    if not tournament:
        raise HTTPException(status_code=404)

    valid_transitions = {
        "SETUP": ["REGISTRATION_OPEN"],
        "REGISTRATION_OPEN": ["SETUP"],
        "ACTIVE": ["FINISHED"],
    }
    allowed = valid_transitions.get(tournament["status"], [])
    if new_status not in allowed:
        _flash(request, f"Transizione non valida: {tournament['status']} -> {new_status}", "error")
        return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)

    if new_status == "FINISHED":
        try:
            await _archive_tournament(tournament)
        except Exception as e:
            _flash(request, f"Torneo terminato, ma archiviazione storico fallita: {e}", "error")
            database.update_tournament_status(tournament_id, new_status)
            return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)

    database.update_tournament_status(tournament_id, new_status)
    _flash(request, f"Stato aggiornato: {new_status}", "ok")
    return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)


# ---------------------------------------------------------------------------
# Admin: shuffle seed and start tournament
# ---------------------------------------------------------------------------

@app.post("/admin/tournament/{tournament_id}/start")
async def tournament_start(request: Request, tournament_id: str, shuffle: str = Form(default="1")):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    tournament = database.get_tournament(tournament_id)
    if not tournament or tournament["status"] != "REGISTRATION_OPEN":
        _flash(request, "Il torneo deve essere in REGISTRATION_OPEN per essere avviato.", "error")
        return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)

    try:
        if shuffle == "1":
            await challonge.randomize_participants(tournament_id)
        await challonge.start_tournament(tournament_id)
        database.update_tournament_status(tournament_id, "ACTIVE")
        msg = "Seed mescolati. Torneo avviato!" if shuffle == "1" else "Torneo avviato con i seed attuali!"
        _flash(request, msg, "ok")
    except Exception as e:
        _flash(request, f"Errore avvio torneo: {e}", "error")

    return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)


# ---------------------------------------------------------------------------
# Admin: add participant manually (senza passare dal bot Discord)
# ---------------------------------------------------------------------------

@app.post("/admin/tournament/{tournament_id}/participant/add")
async def participant_add_manual(
    request: Request,
    tournament_id: str,
    name: str = Form(...),
    tekken_id: str = Form(default=""),
    main_character: str = Form(default=""),
):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    tournament = database.get_tournament(tournament_id)
    if not tournament or tournament["status"] not in ("SETUP", "REGISTRATION_OPEN"):
        _flash(request, "Si possono aggiungere iscritti solo prima dell'avvio del torneo.", "error")
        return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)

    name = name.strip()
    if not name:
        _flash(request, "Il nome del partecipante non può essere vuoto.", "error")
        return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)

    try:
        p = await challonge.add_participant(tournament_id, name, misc=main_character or None)
        # discord_id sintetico: gli iscritti manuali non hanno un profilo Discord collegato
        database.add_participant(
            discord_id=f"manual:{p['id']}",
            discord_name=name,
            tekken_id=tekken_id.strip() or None,
            main_character=main_character.strip(),
            challonge_participant_id=str(p["id"]),
            tournament_id=tournament_id,
        )
        _flash(request, f"Partecipante '{name}' iscritto.", "ok")
    except Exception as e:
        _flash(request, f"Errore iscrizione: {e}", "error")

    return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)


# ---------------------------------------------------------------------------
# Admin: manual seeding (ordine del bracket)
# ---------------------------------------------------------------------------

@app.post("/admin/tournament/{tournament_id}/seeds")
async def tournament_seeds(request: Request, tournament_id: str):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    tournament = database.get_tournament(tournament_id)
    if not tournament or tournament["status"] not in ("SETUP", "REGISTRATION_OPEN"):
        _flash(request, "I seed si possono modificare solo prima dell'avvio del torneo.", "error")
        return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)

    form = await request.form()
    assignments = []
    for key, value in form.items():
        if not key.startswith("seed__"):
            continue
        try:
            assignments.append((key[len("seed__"):], int(value)))
        except (TypeError, ValueError):
            _flash(request, f"Seed non valido: '{value}'. Usa solo numeri.", "error")
            return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)

    seeds = [s for _, s in assignments]
    if sorted(seeds) != list(range(1, len(seeds) + 1)):
        _flash(request, f"I seed devono essere i numeri da 1 a {len(seeds)}, ognuno usato una sola volta.", "error")
        return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)

    try:
        # Applicati in ordine crescente di seed: ogni update su Challonge riordina gli altri
        for participant_id, seed in sorted(assignments, key=lambda a: a[1]):
            await challonge.update_participant_seed(tournament_id, participant_id, seed)
        _flash(request, "Seed aggiornati. Il bracket seguirà questo ordine all'avvio (senza mescolare).", "ok")
    except Exception as e:
        _flash(request, f"Errore aggiornamento seed: {e}", "error")

    return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)


# ---------------------------------------------------------------------------
# Admin: remove participant
# ---------------------------------------------------------------------------

@app.post("/admin/tournament/{tournament_id}/participant/{discord_id}/remove")
async def participant_remove(request: Request, tournament_id: str, discord_id: str):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    participant = database.get_participant(discord_id)
    if not participant or participant["tournament_id"] != tournament_id:
        raise HTTPException(status_code=404)

    try:
        if participant["challonge_participant_id"]:
            await challonge.remove_participant(tournament_id, participant["challonge_participant_id"])
        database.remove_participant(discord_id)
        _flash(request, f"Partecipante {participant['discord_name']} rimosso.", "ok")
    except Exception as e:
        _flash(request, f"Errore rimozione: {e}", "error")

    return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)


# ---------------------------------------------------------------------------
# Admin: edit participant
# ---------------------------------------------------------------------------

@app.post("/admin/tournament/{tournament_id}/participant/{discord_id}/edit")
async def participant_edit(
    request: Request,
    tournament_id: str,
    discord_id: str,
    tekken_id: str = Form(default=""),
    main_character: str = Form(...),
):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    participant = database.get_participant(discord_id)
    if not participant or participant["tournament_id"] != tournament_id:
        raise HTTPException(status_code=404)

    database.update_participant(discord_id, tekken_id=tekken_id or None, main_character=main_character)
    _flash(request, "Partecipante aggiornato.", "ok")
    return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)


# ---------------------------------------------------------------------------
# Admin: remove Challonge-only participant (aggiunto a mano su Challonge)
# ---------------------------------------------------------------------------

@app.post("/admin/tournament/{tournament_id}/challonge-participant/{participant_id}/remove")
async def challonge_participant_remove(request: Request, tournament_id: str, participant_id: str):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    try:
        await challonge.remove_participant(tournament_id, participant_id)
        _flash(request, "Partecipante rimosso da Challonge.", "ok")
    except Exception as e:
        _flash(request, f"Errore rimozione: {e}", "error")

    return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)


# ---------------------------------------------------------------------------
# Admin: reset tournament (azzera il bracket, conserva i partecipanti)
# ---------------------------------------------------------------------------

@app.post("/admin/tournament/{tournament_id}/reset")
async def tournament_reset(request: Request, tournament_id: str):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    tournament = database.get_tournament(tournament_id)
    if not tournament:
        raise HTTPException(status_code=404)
    if tournament["status"] not in ("ACTIVE", "FINISHED"):
        _flash(request, "Si può resettare solo un torneo avviato o terminato.", "error")
        return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)

    try:
        await challonge.reset_tournament(tournament_id)
        database.update_tournament_status(tournament_id, "REGISTRATION_OPEN")
        _flash(request, "Torneo resettato: bracket azzerato, partecipanti conservati. Iscrizioni riaperte.", "ok")
    except Exception as e:
        _flash(request, f"Errore reset: {e}", "error")

    return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)


# ---------------------------------------------------------------------------
# Admin: delete tournament (Challonge + database locale)
# ---------------------------------------------------------------------------

@app.post("/admin/tournament/{tournament_id}/delete")
async def tournament_delete(request: Request, tournament_id: str):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    tournament = database.get_tournament(tournament_id)
    if not tournament:
        raise HTTPException(status_code=404)

    try:
        await challonge.delete_tournament(tournament_id)
    except Exception as e:
        # Se su Challonge non esiste più si procede comunque con la pulizia locale
        if "404" not in str(e):
            _flash(request, f"Errore eliminazione su Challonge: {e}", "error")
            return _redirect(f"/admin/tournament/{tournament_id}", status_code=303)

    database.delete_tournament(tournament_id)
    database.delete_tournament_history(tournament_id)
    _flash(request, f"Torneo '{tournament['name']}' eliminato (incluso dallo storico).", "ok")
    return _redirect("/admin/dashboard", status_code=303)


# ---------------------------------------------------------------------------
# Admin: roster personaggi
# ---------------------------------------------------------------------------

@app.get("/admin/roster", response_class=HTMLResponse)
async def admin_roster(request: Request):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    members = database.get_members_with_characters(include_excluded=True)
    return templates.TemplateResponse(request, "admin/roster.html", {
        "user": user,
        "members": members,
        "all_characters": database.get_all_characters(),
        "default_label": DEFAULT_CHARACTER_LABEL,
        **_pop_flash(request),
    })


def _apply_bulk_roster_edits(form) -> None:
    """Salva alias e personaggi per ogni membro presente nel form del roster
    (campi 'alias__{discord_id}' e 'characters__{discord_id}'), permettendo
    di modificare più utenti insieme con un solo salvataggio."""
    known_ids = {m["discord_id"] for m in database.get_all_members(include_excluded=True)}
    for key in form.keys():
        if not key.startswith("alias__"):
            continue
        discord_id = key[len("alias__"):]
        if discord_id not in known_ids:
            continue
        alias = form.get(key, "").strip()
        characters = form.getlist(f"characters__{discord_id}")
        database.set_member_alias(discord_id, alias)
        database.set_character_assignments(discord_id, characters)


async def _sync_character_roles():
    """Assegna su Discord i ruoli dei personaggi appena salvati. Restituisce un avviso da mostrare
    all'admin se qualcosa non è andato, altrimenti None."""
    try:
        result = await roster.sync_character_roles()
    except Exception as e:
        return f"assegnazione dei ruoli personaggio fallita: {e}"
    if result["missing_permission"]:
        return ("il bot non può assegnare i ruoli personaggio: dagli il permesso \"Gestisci ruoli\" "
                "e tieni il suo ruolo sopra quelli dei personaggi.")
    return None


@app.post("/admin/roster/bulk-edit")
async def admin_roster_bulk_edit(request: Request):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    form = await request.form()
    _apply_bulk_roster_edits(form)
    role_warning = await _sync_character_roles()
    try:
        await roster.sync_all_member_messages()
        if role_warning:
            _flash(request, f"Roster aggiornato su Discord, ma {role_warning}", "error")
        else:
            _flash(request, "Roster aggiornato e sincronizzato su Discord.", "ok")
    except Exception as e:
        _flash(request, f"Dati salvati, ma sync Discord fallita: {e}", "error")

    return _redirect("/admin/roster", status_code=303)


@app.post("/admin/roster/{discord_id}/toggle-exclude")
async def admin_roster_toggle_exclude(request: Request, discord_id: str):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    form = await request.form()
    _apply_bulk_roster_edits(form)

    current = next(
        (m for m in database.get_all_members(include_excluded=True) if m["discord_id"] == discord_id),
        None,
    )
    if current is None:
        raise HTTPException(status_code=404)
    database.set_member_excluded(discord_id, not current["excluded"])
    await _sync_character_roles()

    try:
        await roster.sync_all_member_messages()
        _flash(request, "Visibilità nel roster aggiornata.", "ok")
    except Exception as e:
        _flash(request, f"Preferenza salvata, ma sync Discord fallita: {e}", "error")

    return _redirect("/admin/roster", status_code=303)


# ---------------------------------------------------------------------------
# Admin: lista personaggi Tekken 8
# ---------------------------------------------------------------------------

@app.get("/admin/characters", response_class=HTMLResponse)
async def admin_characters(request: Request):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    characters = database.get_all_characters()
    return templates.TemplateResponse(request, "admin/characters.html", {
        "user": user, "characters": characters, **_pop_flash(request),
    })


@app.post("/admin/characters/add")
async def admin_characters_add(request: Request, name: str = Form(...)):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    name = name.strip()
    if name:
        database.add_character(name)
        _flash(request, f"Personaggio '{name}' aggiunto.", "ok")
    else:
        _flash(request, "Il nome del personaggio non può essere vuoto.", "error")

    return _redirect("/admin/characters", status_code=303)


@app.post("/admin/characters/remove")
async def admin_characters_remove(request: Request, name: str = Form(...)):
    user = require_admin(request)
    if not user:
        return _redirect("/login")

    database.remove_character(name)
    try:
        await roster.sync_all_member_messages()
        _flash(request, f"Personaggio '{name}' rimosso e roster Discord sincronizzato.", "ok")
    except Exception as e:
        _flash(request, f"Personaggio rimosso, ma sync Discord fallita: {e}", "error")

    return _redirect("/admin/characters", status_code=303)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("WEB_PORT", 8000))
    uvicorn.run("web_app:app", host="127.0.0.1", port=port)
