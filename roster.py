import asyncio
import hashlib
import json
import os

import httpx

import database
from constants import DEFAULT_CHARACTER_LABEL

DISCORD_API = "https://discord.com/api"

DEFAULT_COLOR = 0xF1C40F  # Giallo Paul Phoenix
PRO_COLOR = 0x3B82F6  # Blu, per il ruolo Pro

REQUEST_PACING_SECONDS = 0.3
MAX_RATE_LIMIT_RETRIES = 5

_lock = asyncio.Lock()


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
