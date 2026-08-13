import json
import time
import os

import aiohttp
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

CHALLONGE_API_BASE = "https://api.challonge.com/v2.1"
CHALLONGE_TOKEN_URL = "https://api.challonge.com/oauth/token"
# Senza il parametro scope, Challonge concede un token di sola lettura:
# gli scope di scrittura vanno richiesti esplicitamente.
CHALLONGE_SCOPES = (
    "me tournaments:read tournaments:write "
    "matches:read matches:write participants:read participants:write"
)


def _tournament_from_v2(item):
    attrs = item["attributes"]
    return {
        "id": item["id"],
        "url": attrs.get("url"),
        "name": attrs.get("name"),
        "state": attrs.get("state"),
        "tournament_type": attrs.get("tournament_type"),
    }


def _participant_from_v2(item):
    attrs = item["attributes"]
    return {
        "id": item["id"],
        "name": attrs.get("name"),
        "misc": attrs.get("misc"),
        "seed": attrs.get("seed"),
        "final_rank": attrs.get("final_rank"),
    }


def _match_from_v2(item):
    attrs = item["attributes"]
    pbp = attrs.get("points_by_participant") or []
    sets = attrs.get("score_in_sets") or []
    scores_csv = ",".join(f"{s[0]}-{s[1]}" for s in sets if len(s) >= 2) or None
    return {
        "id": item["id"],
        "round": attrs.get("round"),
        "state": attrs.get("state"),
        "player1_id": pbp[0]["participant_id"] if len(pbp) > 0 else None,
        "player2_id": pbp[1]["participant_id"] if len(pbp) > 1 else None,
        "scores_csv": scores_csv,
        "winner_id": attrs.get("winner_id"),
        "tie": attrs.get("tie"),
        "suggested_play_order": attrs.get("suggested_play_order"),
    }


class ChallongeClient:
    """Client per l'API Challonge v2.1 (OAuth2 client_credentials).

    L'API v2.1 usa il formato JSON:API (data/type/attributes): qui le risposte
    vengono appiattite in dizionari stile v1, così bot.py e web_app.py possono
    continuare a usare chiavi come player1_id, scores_csv, winner_id, url."""

    def __init__(self):
        self.client_id = os.getenv("CHALLONGE_CLIENT_ID", "")
        self.client_secret = os.getenv("CHALLONGE_CLIENT_SECRET", "")
        self._access_token: str | None = None
        self._token_expires_at: float = 0

    async def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        async with aiohttp.ClientSession() as session:
            async with session.post(
                CHALLONGE_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": CHALLONGE_SCOPES,
                },
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise Exception(f"Challonge OAuth error {resp.status}: {text}")
                data = await resp.json()

        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 3600)
        return self._access_token

    async def _request(self, method: str, endpoint: str, payload=None, params=None):
        token = await self._get_access_token()
        url = f"{CHALLONGE_API_BASE}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Authorization-Type": "v2",
            "Content-Type": "application/vnd.api+json",
            "Accept": "application/json",
        }
        body = json.dumps(payload) if payload is not None else None

        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, headers=headers, data=body, params=params) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise Exception(f"Challonge API {resp.status}: {text}")
                if resp.status == 204:
                    return None
                try:
                    return await resp.json()
                except Exception:
                    return None

    async def _get_all_pages(self, endpoint: str, params=None):
        """Le liste v2.1 sono paginate (default 25 per pagina): scarica tutte le pagine.
        Alcuni endpoint (es. matches) ignorano page/per_page e restituiscono sempre
        tutto: ci si ferma appena una pagina non porta id nuovi."""
        items = []
        seen_ids = set()
        page = 1
        while page <= 50:
            page_params = dict(params or {})
            page_params.update({"page": page, "per_page": 100})
            result = await self._request("GET", endpoint, params=page_params)
            batch = (result or {}).get("data") or []
            new_items = [i for i in batch if i.get("id") not in seen_ids]
            if not new_items:
                break
            seen_ids.update(i.get("id") for i in new_items)
            items.extend(new_items)
            page += 1
        return items

    async def create_tournament(self, name: str, tournament_type: str, url_slug: str = None):
        attrs = {"name": name, "tournament_type": tournament_type}
        if url_slug:
            attrs["url"] = url_slug
        payload = {"data": {"type": "tournament", "attributes": attrs}}
        result = await self._request("POST", "tournaments.json", payload=payload)
        return _tournament_from_v2(result["data"])

    async def get_tournament(self, tournament_id: str):
        result = await self._request("GET", f"tournaments/{tournament_id}.json")
        return _tournament_from_v2(result["data"])

    async def list_tournaments(self):
        items = await self._get_all_pages("tournaments.json")
        return [_tournament_from_v2(i) for i in items]

    async def add_participant(self, tournament_id: str, name: str, misc: str = None):
        attrs = {"name": name}
        if misc:
            attrs["misc"] = misc
        payload = {"data": {"type": "participant", "attributes": attrs}}
        result = await self._request(
            "POST", f"tournaments/{tournament_id}/participants.json", payload=payload
        )
        return _participant_from_v2(result["data"])

    async def remove_participant(self, tournament_id: str, participant_id: str):
        await self._request(
            "DELETE", f"tournaments/{tournament_id}/participants/{participant_id}.json"
        )

    async def list_participants(self, tournament_id: str):
        items = await self._get_all_pages(f"tournaments/{tournament_id}/participants.json")
        return [_participant_from_v2(i) for i in items]

    async def update_participant_seed(self, tournament_id: str, participant_id: str, seed: int):
        payload = {"data": {"type": "participant", "attributes": {"seed": int(seed)}}}
        result = await self._request(
            "PUT", f"tournaments/{tournament_id}/participants/{participant_id}.json", payload=payload
        )
        return _participant_from_v2(result["data"]) if result and result.get("data") else None

    async def randomize_participants(self, tournament_id: str):
        await self._request(
            "PUT", f"tournaments/{tournament_id}/participants/randomize.json"
        )

    async def start_tournament(self, tournament_id: str):
        payload = {"data": {"type": "TournamentState", "attributes": {"state": "start"}}}
        result = await self._request(
            "PUT", f"tournaments/{tournament_id}/change_state.json", payload=payload
        )
        return _tournament_from_v2(result["data"])

    async def finalize_tournament(self, tournament_id: str):
        """Chiude ufficialmente il torneo su Challonge (assegna i final_rank)."""
        payload = {"data": {"type": "TournamentState", "attributes": {"state": "finalize"}}}
        result = await self._request(
            "PUT", f"tournaments/{tournament_id}/change_state.json", payload=payload
        )
        return _tournament_from_v2(result["data"])

    async def reset_tournament(self, tournament_id: str):
        """Riporta un torneo avviato allo stato pre-avvio (i partecipanti restano)."""
        payload = {"data": {"type": "TournamentState", "attributes": {"state": "reset"}}}
        result = await self._request(
            "PUT", f"tournaments/{tournament_id}/change_state.json", payload=payload
        )
        return _tournament_from_v2(result["data"])

    async def delete_tournament(self, tournament_id: str):
        await self._request("DELETE", f"tournaments/{tournament_id}.json")

    async def get_matches(self, tournament_id: str, state: str = None):
        params = {"state": state} if state else None
        items = await self._get_all_pages(f"tournaments/{tournament_id}/matches.json", params=params)
        return [_match_from_v2(i) for i in items]

    async def report_match(
        self, tournament_id: str, match_id: str, winner_id: str, scores_csv: str
    ):
        # Il formato v2.1 vuole i punteggi per-partecipante, non la stringa "x-y":
        # recupera il match per sapere chi è player1 e player2.
        result = await self._request(
            "GET", f"tournaments/{tournament_id}/matches/{match_id}.json"
        )
        match = _match_from_v2(result["data"])

        sets = [s.split("-") for s in scores_csv.split(",")]
        p1_scores = ",".join(s[0].strip() for s in sets)
        p2_scores = ",".join(s[1].strip() for s in sets)

        entries = []
        for pid, score_set in ((match["player1_id"], p1_scores), (match["player2_id"], p2_scores)):
            entry = {"participant_id": str(pid), "score_set": score_set}
            if str(pid) == str(winner_id):
                entry["advancing"] = True
            entries.append(entry)

        payload = {"data": {"type": "Match", "attributes": {"match": entries}}}
        result = await self._request(
            "PUT", f"tournaments/{tournament_id}/matches/{match_id}.json", payload=payload
        )
        return _match_from_v2(result["data"]) if result and result.get("data") else None


challonge = ChallongeClient()
