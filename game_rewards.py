"""Persistent postcard rewards for Shoot a barev."""
from __future__ import annotations

import secrets
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

CONTACT_REQUEST_AGE = 15 * 60
MARKETING_VERSION = "barev-telegram-marketing-v1"
MARKETING_TEXT = "Я согласен(-на) получать в Telegram новости, акции и предложения о подарках от BAREV, JANIK. Отказаться можно командой /stop."


def phone_key(phone_number: str) -> str:
    digits = re.sub(r"[^0-9]", "", phone_number)
    if digits.startswith("00"):
        digits = digits[2:]
    if not 7 <= len(digits) <= 15:
        raise ValueError("Invalid phone number")
    return digits


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
                CREATE TABLE IF NOT EXISTS postcard_phone_rewards (
                    phone_key TEXT PRIMARY KEY,
                    code TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS postcard_telegram_rewards (
                    telegram_id INTEGER PRIMARY KEY,
                    code TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS marketing_preferences (
                    telegram_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS marketing_consent_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    source TEXT NOT NULL,
                    version TEXT NOT NULL,
                    consent_text TEXT NOT NULL,
                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS postcard_reward_notifications (
                    code TEXT NOT NULL,
                    admin_id INTEGER NOT NULL,
                    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (code, admin_id)
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            pending_columns = {row["name"] for row in db.execute("PRAGMA table_info(pending_game_claims)")}
            if "marketing_opt_in" not in pending_columns:
                db.execute("ALTER TABLE pending_game_claims ADD COLUMN marketing_opt_in INTEGER NOT NULL DEFAULT 0")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(postcard_rewards)")}
            for name in ("telegram_username", "phone_number"):
                if name not in columns:
                    db.execute(f"ALTER TABLE postcard_rewards ADD COLUMN {name} TEXT")
            if "qr_sent_at" not in columns:
                db.execute("ALTER TABLE postcard_rewards ADD COLUMN qr_sent_at TEXT")
                # Existing prizes predate delivery tracking and already have a QR.
                db.execute("UPDATE postcard_rewards SET qr_sent_at = issued_at")
            old_rewards = db.execute(
                "SELECT code, phone_number FROM postcard_rewards WHERE phone_number IS NOT NULL "
                "ORDER BY (redeemed_at IS NULL), issued_at, telegram_id"
            ).fetchall()
            for reward in old_rewards:
                try:
                    normalized = phone_key(reward["phone_number"])
                except ValueError:
                    continue
                db.execute(
                    "INSERT INTO postcard_phone_rewards (phone_key, code) VALUES (?, ?) "
                    "ON CONFLICT (phone_key) DO NOTHING", (normalized, reward["code"]),
                )

            # Preserve the account limit even when several Telegram accounts used one phone.
            for account in db.execute("SELECT telegram_id, code FROM postcard_rewards").fetchall():
                canonical = self._reward_by_code(db, account["code"])
                db.execute(
                    "INSERT INTO postcard_telegram_rewards (telegram_id, code) VALUES (?, ?) "
                    "ON CONFLICT (telegram_id) DO NOTHING",
                    (account["telegram_id"], canonical["code"]),
                )

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def record_verified_win(self):
        ticket = secrets.token_urlsafe(24)
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO game_wins (ticket) VALUES (?)", (ticket,))
        return ticket

    def begin_claim(self, ticket: str, telegram_id: int, marketing_opt_in: bool = False) -> bool:
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            win = db.execute("SELECT claimed_by FROM game_wins WHERE ticket = ?", (ticket,)).fetchone()
            if win is None or win["claimed_by"] not in (None, telegram_id):
                return False
            db.execute("DELETE FROM pending_game_claims WHERE expires <= ?", (now,))
            db.execute(
                "INSERT INTO pending_game_claims (telegram_id, ticket, expires, marketing_opt_in) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (telegram_id) DO UPDATE SET ticket = excluded.ticket, expires = excluded.expires, "
                "marketing_opt_in = excluded.marketing_opt_in",
                (telegram_id, ticket, now + CONTACT_REQUEST_AGE, int(marketing_opt_in is True)),
            )
            return True

    def claim_after_consent(self, telegram_id: int, username: str | None, phone_number: str):
        """Return the existing or newly issued reward, plus whether it is new."""
        normalized = phone_key(phone_number)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute(
                "SELECT ticket, marketing_opt_in FROM pending_game_claims WHERE telegram_id = ? AND expires > ?",
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
            self._set_marketing(db, telegram_id, bool(pending["marketing_opt_in"]), "prize_contact")
            # Check Telegram ID first; changing its phone cannot issue a second prize.
            account = db.execute(
                "SELECT code FROM postcard_telegram_rewards WHERE telegram_id = ?", (telegram_id,),
            ).fetchone()
            phone_reward = db.execute(
                "SELECT r.* FROM postcard_phone_rewards p JOIN postcard_rewards r ON r.code = p.code "
                "WHERE p.phone_key = ?", (normalized,),
            ).fetchone()
            if account:
                existing = self._reward_by_code(db, account["code"])
                db.execute(
                    "UPDATE postcard_rewards SET telegram_username = ? WHERE telegram_id = ?",
                    (username, telegram_id),
                )
                if phone_reward is None:
                    db.execute(
                        "INSERT INTO postcard_phone_rewards (phone_key, code) VALUES (?, ?)",
                        (normalized, existing["code"]),
                    )
                return dict(self._reward_by_code(db, existing["code"])), False
            if phone_reward:
                existing = self._reward_by_code(db, phone_reward["code"])
                db.execute(
                    "INSERT INTO postcard_telegram_rewards (telegram_id, code) VALUES (?, ?)",
                    (telegram_id, existing["code"]),
                )
                return dict(existing), False
            db.execute(
                "INSERT INTO postcard_rewards (telegram_id, code, telegram_username, phone_number) VALUES (?, ?, ?, ?)",
                (telegram_id, secrets.token_hex(12).upper(), username, phone_number),
            )
            reward = db.execute(
                "SELECT * FROM postcard_rewards WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            db.execute(
                "INSERT INTO postcard_phone_rewards (phone_key, code) VALUES (?, ?)",
                (normalized, reward["code"]),
            )
            db.execute(
                "INSERT INTO postcard_telegram_rewards (telegram_id, code) VALUES (?, ?)",
                (telegram_id, reward["code"]),
            )
            return dict(reward), True

    @staticmethod
    def _set_marketing(db, telegram_id, enabled, source):
        db.execute(
            "INSERT INTO marketing_preferences (telegram_id, enabled) VALUES (?, ?) "
            "ON CONFLICT (telegram_id) DO UPDATE SET enabled = excluded.enabled, updated_at = CURRENT_TIMESTAMP",
            (telegram_id, int(enabled)),
        )
        db.execute(
            "INSERT INTO marketing_consent_events (telegram_id, enabled, source, version, consent_text) "
            "VALUES (?, ?, ?, ?, ?)",
            (telegram_id, int(enabled), source, MARKETING_VERSION, MARKETING_TEXT),
        )

    def stop_marketing(self, telegram_id):
        with closing(self.connect()) as db, db:
            self._set_marketing(db, telegram_id, False, "telegram_stop")
            db.execute("UPDATE pending_game_claims SET marketing_opt_in = 0 WHERE telegram_id = ?", (telegram_id,))

    def marketing_enabled(self, telegram_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT enabled FROM marketing_preferences WHERE telegram_id = ?", (telegram_id,)).fetchone()
            return bool(row and row["enabled"])

    def mark_qr_sent(self, code):
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE postcard_rewards SET qr_sent_at = COALESCE(qr_sent_at, CURRENT_TIMESTAMP) WHERE code = ?",
                (code,),
            )

    def reward_for_telegram(self, telegram_id):
        with closing(self.connect()) as db:
            account = db.execute(
                "SELECT code FROM postcard_telegram_rewards WHERE telegram_id = ?", (telegram_id,),
            ).fetchone()
            reward = self._reward_by_code(db, account["code"]) if account else None
            return dict(reward) if reward else None

    @staticmethod
    def _reward_by_code(db, code):
        reward = db.execute("SELECT * FROM postcard_rewards WHERE code = ?", (code,)).fetchone()
        if reward is None or not reward["phone_number"]:
            return reward
        try:
            normalized = phone_key(reward["phone_number"])
        except ValueError:
            return reward
        canonical = db.execute(
            "SELECT r.* FROM postcard_phone_rewards p JOIN postcard_rewards r ON r.code = p.code "
            "WHERE p.phone_key = ?", (normalized,),
        ).fetchone()
        return canonical or reward

    def reward_by_code(self, code):
        with closing(self.connect()) as db:
            reward = self._reward_by_code(db, code)
            return dict(reward) if reward else None

    def pending_notifications(self, admin_id):
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT DISTINCT r.* FROM postcard_phone_rewards p "
                "JOIN postcard_rewards r ON r.code = p.code "
                "LEFT JOIN postcard_reward_notifications n ON n.code = r.code AND n.admin_id = ? "
                "WHERE n.code IS NULL AND r.redeemed_at IS NULL ORDER BY r.issued_at LIMIT 50",
                (admin_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_notified(self, code, admin_id):
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO postcard_reward_notifications (code, admin_id) VALUES (?, ?) "
                "ON CONFLICT (code, admin_id) DO NOTHING", (code, admin_id),
            )

    def list_rewards(self):
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT * FROM postcard_rewards ORDER BY issued_at DESC"
            ).fetchall()
            unique = {}
            for row in rows:
                reward = self._reward_by_code(db, row["code"])
                unique.setdefault(reward["code"], dict(reward))
            return list(unique.values())

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
            reward = self._reward_by_code(db, code)
            if reward is None:
                return "unknown"
            code = reward["code"]
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
