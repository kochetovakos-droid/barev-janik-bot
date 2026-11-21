"""Persistent postcard rewards for Shoot a barev."""
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path


class Rewards:
    def __init__(self, path: Path):
        self.path = path
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS game_wins (
                    ticket TEXT PRIMARY KEY,
                    claimed_by INTEGER
                );
                CREATE TABLE IF NOT EXISTS postcard_rewards (
                    telegram_id INTEGER PRIMARY KEY,
                    code TEXT NOT NULL UNIQUE,
                    consented_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    issued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    redeemed_at TEXT,
                    redeemed_by INTEGER
                );
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def record_verified_win(self):
        ticket = secrets.token_urlsafe(24)
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO game_wins (ticket) VALUES (?)", (ticket,))
        return ticket

    def claim_after_consent(self, ticket: str, telegram_id: int):
        """Return the existing or newly issued reward, plus whether it is new."""
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            win = db.execute(
                "SELECT claimed_by FROM game_wins WHERE ticket = ?", (ticket,)
            ).fetchone()
            if win is None or win["claimed_by"] not in (None, telegram_id):
                return None, False
            db.execute(
                "UPDATE game_wins SET claimed_by = ? WHERE ticket = ?",
                (telegram_id, ticket),
            )
            created = db.execute(
                "INSERT INTO postcard_rewards (telegram_id, code) VALUES (?, ?) "
                "ON CONFLICT (telegram_id) DO NOTHING",
                (telegram_id, secrets.token_hex(12).upper()),
            ).rowcount == 1
            reward = db.execute(
                "SELECT * FROM postcard_rewards WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            return dict(reward), created

    def redeem(self, code: str, admin_id: int):
        """Call only after the current Telegram user passes the admin check."""
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE postcard_rewards SET redeemed_at = CURRENT_TIMESTAMP, "
                "redeemed_by = ? WHERE code = ? AND redeemed_at IS NULL",
                (admin_id, code),
            ).rowcount
            if changed:
                return "redeemed"
            exists = db.execute(
                "SELECT 1 FROM postcard_rewards WHERE code = ?", (code,)
            ).fetchone()
            return "already_redeemed" if exists else "unknown"
