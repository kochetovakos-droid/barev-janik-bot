"""Persistent postcard rewards for Shoot a barev."""
from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

CONTACT_REQUEST_AGE = 15 * 60


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
                    redeemed_by INTEGER,
                    telegram_username TEXT,
                    phone_number TEXT
                );
                CREATE TABLE IF NOT EXISTS pending_game_claims (
                    telegram_id INTEGER PRIMARY KEY,
                    ticket TEXT NOT NULL,
                    expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS game_reward_admins (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(postcard_rewards)")}
            for name in ("telegram_username", "phone_number"):
                if name not in columns:
                    db.execute(f"ALTER TABLE postcard_rewards ADD COLUMN {name} TEXT")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def record_verified_win(self):
        ticket = secrets.token_urlsafe(24)
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO game_wins (ticket) VALUES (?)", (ticket,))
        return ticket

    def begin_claim(self, ticket: str, telegram_id: int) -> bool:
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            win = db.execute("SELECT claimed_by FROM game_wins WHERE ticket = ?", (ticket,)).fetchone()
            if win is None or win["claimed_by"] not in (None, telegram_id):
                return False
            db.execute("DELETE FROM pending_game_claims WHERE expires <= ?", (now,))
            db.execute(
                "INSERT INTO pending_game_claims (telegram_id, ticket, expires) VALUES (?, ?, ?) "
                "ON CONFLICT (telegram_id) DO UPDATE SET ticket = excluded.ticket, expires = excluded.expires",
                (telegram_id, ticket, now + CONTACT_REQUEST_AGE),
            )
            return True

    def claim_after_consent(self, telegram_id: int, username: str | None, phone_number: str):
        """Return the existing or newly issued reward, plus whether it is new."""
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute(
                "SELECT ticket FROM pending_game_claims WHERE telegram_id = ? AND expires > ?",
                (telegram_id, time.time()),
            ).fetchone()
            if pending is None:
                return None, False
            db.execute("DELETE FROM pending_game_claims WHERE telegram_id = ?", (telegram_id,))
            ticket = pending["ticket"]
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
                "INSERT INTO postcard_rewards (telegram_id, code, telegram_username, phone_number) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (telegram_id) DO NOTHING",
                (telegram_id, secrets.token_hex(12).upper(), username, phone_number),
            ).rowcount == 1
            if not created:
                db.execute(
                    "UPDATE postcard_rewards SET telegram_username = ?, phone_number = ?, "
                    "consented_at = CASE WHEN phone_number IS NULL THEN CURRENT_TIMESTAMP ELSE consented_at END "
                    "WHERE telegram_id = ?",
                    (username, phone_number, telegram_id),
                )
            reward = db.execute(
                "SELECT * FROM postcard_rewards WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            return dict(reward), created

    def remember_admin(self, telegram_id: int, username: str | None):
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO game_reward_admins (telegram_id, username) VALUES (?, ?) "
                "ON CONFLICT (telegram_id) DO UPDATE SET username = excluded.username",
                (telegram_id, username.lower() if username else None),
            )

    def admin_chats(self, admins):
        with closing(self.connect()) as db:
            saved = db.execute("SELECT * FROM game_reward_admins").fetchall()
        return {int(admin) for admin in admins if admin.isdecimal()} | {
            row["telegram_id"] for row in saved if row["username"] in admins
        }

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
