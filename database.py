import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tournament.db")


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tournaments (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'SETUP',
                rules_ft INTEGER NOT NULL DEFAULT 2
            );
            CREATE TABLE IF NOT EXISTS participants (
                discord_id TEXT PRIMARY KEY,
                discord_name TEXT NOT NULL,
                tekken_id TEXT,
                main_character TEXT NOT NULL,
                challonge_participant_id TEXT,
                tournament_id TEXT NOT NULL,
                FOREIGN KEY (tournament_id) REFERENCES tournaments(id)
            );
            CREATE TABLE IF NOT EXISTS members (
                discord_id TEXT PRIMARY KEY,
                discord_name TEXT NOT NULL,
                is_bot INTEGER NOT NULL DEFAULT 0,
                excluded INTEGER NOT NULL DEFAULT 0,
                alias TEXT,
                is_pro INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS character_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT NOT NULL REFERENCES members(discord_id),
                character_name TEXT NOT NULL,
                UNIQUE(discord_id, character_name)
            );
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS tekken_characters (
                name TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS roster_messages (
                discord_id TEXT PRIMARY KEY REFERENCES members(discord_id),
                message_id TEXT NOT NULL,
                content_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS tournament_history (
                tournament_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                finished_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tournament_placements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id TEXT NOT NULL REFERENCES tournament_history(tournament_id),
                player_key TEXT NOT NULL,
                display_name TEXT NOT NULL,
                final_rank INTEGER,
                main_character TEXT
            );
            CREATE TABLE IF NOT EXISTS match_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id TEXT NOT NULL REFERENCES tournament_history(tournament_id),
                round INTEGER,
                play_order INTEGER,
                winner_key TEXT NOT NULL,
                winner_name TEXT NOT NULL,
                loser_key TEXT NOT NULL,
                loser_name TEXT NOT NULL,
                scores_csv TEXT
            );
        """)
        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(members)").fetchall()}
        if "excluded" not in existing_columns:
            conn.execute("ALTER TABLE members ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
        if "alias" not in existing_columns:
            conn.execute("ALTER TABLE members ADD COLUMN alias TEXT")
        if "is_pro" not in existing_columns:
            conn.execute("ALTER TABLE members ADD COLUMN is_pro INTEGER NOT NULL DEFAULT 0")

        character_count = conn.execute("SELECT COUNT(*) FROM tekken_characters").fetchone()[0]
        if character_count == 0:
            from constants import TEKKEN_CHARACTERS
            conn.executemany(
                "INSERT OR IGNORE INTO tekken_characters (name) VALUES (?)",
                [(name,) for name in TEKKEN_CHARACTERS],
            )


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- Tournament helpers ---

def get_open_tournament():
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tournaments WHERE status = 'REGISTRATION_OPEN' LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_active_tournament():
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tournaments WHERE status = 'ACTIVE' LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_tournament(tournament_id: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
        ).fetchone()
        return dict(row) if row else None


def get_all_tournaments():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tournaments ORDER BY rowid DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def create_tournament(id: str, name: str, status: str, rules_ft: int):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO tournaments (id, name, status, rules_ft) VALUES (?, ?, ?, ?)",
            (id, name, status, rules_ft)
        )


def update_tournament_status(tournament_id: str, status: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE tournaments SET status = ? WHERE id = ?",
            (status, tournament_id)
        )


def delete_tournament(tournament_id: str):
    with get_db() as conn:
        conn.execute("DELETE FROM participants WHERE tournament_id = ?", (tournament_id,))
        conn.execute("DELETE FROM tournaments WHERE id = ?", (tournament_id,))


# --- Participant helpers ---

def get_participant(discord_id: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM participants WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        return dict(row) if row else None


def get_participants_by_tournament(tournament_id: str):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM participants WHERE tournament_id = ?", (tournament_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def add_participant(
    discord_id: str,
    discord_name: str,
    tekken_id,
    main_character: str,
    challonge_participant_id: str,
    tournament_id: str,
):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO participants
               (discord_id, discord_name, tekken_id, main_character, challonge_participant_id, tournament_id)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(discord_id) DO UPDATE SET
               discord_name=excluded.discord_name,
               tekken_id=excluded.tekken_id,
               main_character=excluded.main_character,
               challonge_participant_id=excluded.challonge_participant_id,
               tournament_id=excluded.tournament_id""",
            (discord_id, discord_name, tekken_id, main_character, challonge_participant_id, tournament_id),
        )


def remove_participant(discord_id: str):
    with get_db() as conn:
        conn.execute("DELETE FROM participants WHERE discord_id = ?", (discord_id,))


def update_participant(discord_id: str, tekken_id=None, main_character=None):
    with get_db() as conn:
        if tekken_id is not None:
            conn.execute(
                "UPDATE participants SET tekken_id = ? WHERE discord_id = ?",
                (tekken_id, discord_id),
            )
        if main_character is not None:
            conn.execute(
                "UPDATE participants SET main_character = ? WHERE discord_id = ?",
                (main_character, discord_id),
            )


# --- Storico tornei / Elo ---

def archive_tournament_reset(tournament_id: str, name: str, finished_at: str):
    """Crea (o ricrea da zero, se già presente) la voce di storico per un torneo:
    rende l'archiviazione idempotente se un torneo viene terminato più volte."""
    with get_db() as conn:
        conn.execute("DELETE FROM tournament_placements WHERE tournament_id = ?", (tournament_id,))
        conn.execute("DELETE FROM match_history WHERE tournament_id = ?", (tournament_id,))
        conn.execute(
            """INSERT INTO tournament_history (tournament_id, name, finished_at) VALUES (?, ?, ?)
               ON CONFLICT(tournament_id) DO UPDATE SET name=excluded.name, finished_at=excluded.finished_at""",
            (tournament_id, name, finished_at),
        )


def add_placement(tournament_id: str, player_key: str, display_name: str, final_rank, main_character):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO tournament_placements
               (tournament_id, player_key, display_name, final_rank, main_character)
               VALUES (?, ?, ?, ?, ?)""",
            (tournament_id, player_key, display_name, final_rank, main_character),
        )


def add_match_history(tournament_id: str, round, play_order, winner_key: str, winner_name: str,
                      loser_key: str, loser_name: str, scores_csv):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO match_history
               (tournament_id, round, play_order, winner_key, winner_name, loser_key, loser_name, scores_csv)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (tournament_id, round, play_order, winner_key, winner_name, loser_key, loser_name, scores_csv),
        )


def get_tournament_history():
    """Tornei archiviati (dal più recente), ciascuno con la sua Top 8."""
    with get_db() as conn:
        tournaments = [dict(r) for r in conn.execute(
            "SELECT * FROM tournament_history ORDER BY finished_at DESC"
        ).fetchall()]
        for t in tournaments:
            t["top8"] = [dict(r) for r in conn.execute(
                """SELECT * FROM tournament_placements
                   WHERE tournament_id = ? AND final_rank IS NOT NULL AND final_rank <= 8
                   ORDER BY final_rank ASC, display_name ASC""",
                (t["tournament_id"],),
            ).fetchall()]
        return tournaments


def get_all_history_matches():
    """Tutti i match archiviati in ordine cronologico (per il calcolo dell'Elo)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT m.* FROM match_history m
               JOIN tournament_history h ON h.tournament_id = m.tournament_id
               ORDER BY h.finished_at ASC, m.play_order ASC, m.id ASC"""
        ).fetchall()
        return [dict(r) for r in rows]


def delete_tournament_history(tournament_id: str):
    with get_db() as conn:
        conn.execute("DELETE FROM tournament_placements WHERE tournament_id = ?", (tournament_id,))
        conn.execute("DELETE FROM match_history WHERE tournament_id = ?", (tournament_id,))
        conn.execute("DELETE FROM tournament_history WHERE tournament_id = ?", (tournament_id,))


# --- Member / roster helpers ---

def upsert_member(discord_id: str, discord_name: str, is_bot: bool = False, is_pro: bool = False):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO members (discord_id, discord_name, is_bot, is_pro)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(discord_id) DO UPDATE SET
               discord_name=excluded.discord_name,
               is_bot=excluded.is_bot,
               is_pro=excluded.is_pro""",
            (discord_id, discord_name, int(is_bot), int(is_pro)),
        )


def remove_member(discord_id: str):
    with get_db() as conn:
        conn.execute("DELETE FROM character_assignments WHERE discord_id = ?", (discord_id,))
        conn.execute("DELETE FROM roster_messages WHERE discord_id = ?", (discord_id,))
        conn.execute("DELETE FROM members WHERE discord_id = ?", (discord_id,))


def get_all_members(include_bots: bool = False, include_excluded: bool = False):
    with get_db() as conn:
        conditions = []
        if not include_bots:
            conditions.append("is_bot = 0")
        if not include_excluded:
            conditions.append("excluded = 0")
        query = "SELECT * FROM members"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY COALESCE(alias, discord_name) COLLATE NOCASE"
        rows = conn.execute(query).fetchall()
        return [dict(r) for r in rows]


def prune_members_not_in(current_discord_ids: list):
    with get_db() as conn:
        existing_ids = {r["discord_id"] for r in conn.execute("SELECT discord_id FROM members").fetchall()}
        stale_ids = existing_ids - set(current_discord_ids)
        for discord_id in stale_ids:
            conn.execute("DELETE FROM character_assignments WHERE discord_id = ?", (discord_id,))
            conn.execute("DELETE FROM roster_messages WHERE discord_id = ?", (discord_id,))
            conn.execute("DELETE FROM members WHERE discord_id = ?", (discord_id,))


def set_member_excluded(discord_id: str, excluded: bool):
    with get_db() as conn:
        conn.execute(
            "UPDATE members SET excluded = ? WHERE discord_id = ?",
            (int(excluded), discord_id),
        )


def set_member_alias(discord_id: str, alias: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE members SET alias = ? WHERE discord_id = ?",
            (alias or None, discord_id),
        )


# --- Tekken character list helpers ---

def get_all_characters():
    with get_db() as conn:
        rows = conn.execute("SELECT name FROM tekken_characters ORDER BY name COLLATE NOCASE").fetchall()
        return [r["name"] for r in rows]


def add_character(name: str):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO tekken_characters (name) VALUES (?)", (name,))


def remove_character(name: str):
    with get_db() as conn:
        conn.execute("DELETE FROM character_assignments WHERE character_name = ?", (name,))
        conn.execute("DELETE FROM tekken_characters WHERE name = ?", (name,))


def get_character_assignments(discord_id: str):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT character_name FROM character_assignments WHERE discord_id = ? ORDER BY character_name",
            (discord_id,),
        ).fetchall()
        return [r["character_name"] for r in rows]


def get_members_with_characters(include_bots: bool = False, include_excluded: bool = False):
    members = get_all_members(include_bots=include_bots, include_excluded=include_excluded)
    with get_db() as conn:
        rows = conn.execute("SELECT discord_id, character_name FROM character_assignments").fetchall()
    by_member = {}
    for r in rows:
        by_member.setdefault(r["discord_id"], []).append(r["character_name"])
    for m in members:
        m["characters"] = sorted(by_member.get(m["discord_id"], []))
    return members


def set_character_assignments(discord_id: str, character_names: list):
    with get_db() as conn:
        conn.execute("DELETE FROM character_assignments WHERE discord_id = ?", (discord_id,))
        for name in character_names:
            conn.execute(
                "INSERT INTO character_assignments (discord_id, character_name) VALUES (?, ?)",
                (discord_id, name),
            )


# --- Generic key/value bot state ---

def get_bot_state(key: str):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_bot_state(key: str, value: str):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO bot_state (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )


# --- Roster messages (un messaggio Discord per membro) ---

def get_roster_messages():
    with get_db() as conn:
        rows = conn.execute("SELECT discord_id, message_id, content_hash FROM roster_messages").fetchall()
        return {r["discord_id"]: {"message_id": r["message_id"], "content_hash": r["content_hash"]} for r in rows}


def set_roster_message(discord_id: str, message_id: str, content_hash: str):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO roster_messages (discord_id, message_id, content_hash) VALUES (?, ?, ?)
               ON CONFLICT(discord_id) DO UPDATE SET
               message_id=excluded.message_id,
               content_hash=excluded.content_hash""",
            (discord_id, message_id, content_hash),
        )


def clear_roster_messages():
    with get_db() as conn:
        conn.execute("DELETE FROM roster_messages")


if __name__ == "__main__":
    init_db()
    print(f"Database inizializzato: {DB_PATH}")
