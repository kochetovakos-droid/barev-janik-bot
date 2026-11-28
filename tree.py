"""Dress the tree: second game. Shares the prize pool with Shoot a barev.

Play is unlimited. Every finished tree gets a ticket in game_wins, but the
Telegram claim (game_rewards.claim_after_consent) issues one paper postcard
per phone number and per Telegram account, whichever game the ticket came from.
"""
import re
import math
import secrets
import time
from contextlib import closing
from pathlib import Path

from aiohttp import web

TREE_TEMPLATE = Path(__file__).with_name("tree.html")
COOKIE_NAME = "barev_tree"
SESSION_AGE = 3600
MIN_SECONDS = 6
ORNAMENT_COUNT = 8
TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{32}")


def complete_tree(ornaments):
    """Accept eight separate ornaments hung on the visible branches."""
    if not isinstance(ornaments, list) or len(ornaments) != ORNAMENT_COUNT:
        return False
    points = []
    for ornament in ornaments:
        if not isinstance(ornament, dict):
            return False
        x, y = ornament.get("x"), ornament.get("y")
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in (x, y)):
            return False
        if not any(top + 10 < y < bottom - 6 and abs(x - 170) <
                   (y - top) / (bottom - top) * width - 10
                   for top, bottom, width in ((140, 262, 74), (205, 352, 110), (280, 455, 146))):
            return False
        if any(math.hypot(x - px, y - py) < 37.4 for px, py in points):
            return False
        points.append((x, y))
    return True


class Tree:
    def __init__(self, rewards, bot_username):
        self.rewards = rewards
        self.bot_username = bot_username
        with closing(rewards.connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS tree_sessions (
                    token TEXT PRIMARY KEY,
                    started REAL NOT NULL,
                    expires REAL NOT NULL,
                    ticket TEXT
                )
            """)

    def claim(self, ticket):
        return f"https://t.me/{self.bot_username()}?start=win_{ticket}" if ticket else None

    async def page(self, request):
        if request.path == "/tree":
            raise web.HTTPFound("/?view=menu#tree")
        return web.FileResponse(TREE_TEMPLATE, headers={"Cache-Control": "no-cache"})

    async def start(self, request):
        now = time.time()
        token = secrets.token_urlsafe(24)
        with closing(self.rewards.connect()) as db, db:
            db.execute("DELETE FROM tree_sessions WHERE expires <= ?", (now,))
            db.execute(
                "INSERT INTO tree_sessions (token, started, expires) VALUES (?, ?, ?)",
                (token, now, now + SESSION_AGE),
            )
        response = web.json_response({"ok": True}, headers={"Cache-Control": "no-store"})
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_AGE,
                            secure=True, httponly=True, samesite="Strict", path="/tree")
        return response

    async def win(self, request):
        token = request.cookies.get(COOKIE_NAME, "")
        if not TOKEN_FORMAT.fullmatch(token):
            raise web.HTTPUnauthorized()
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest()
        if not isinstance(payload, dict) or not complete_tree(payload.get("ornaments")):
            raise web.HTTPBadRequest()
        now = time.time()
        with closing(self.rewards.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM tree_sessions WHERE token = ? AND expires > ?", (token, now)
            ).fetchone()
            if not row:
                raise web.HTTPUnauthorized()
            if now - row["started"] < MIN_SECONDS:
                raise web.HTTPBadRequest()
            ticket = row["ticket"]
            if not ticket:
                ticket = secrets.token_urlsafe(24)
                db.execute("INSERT INTO game_wins (ticket) VALUES (?)", (ticket,))
                db.execute("UPDATE tree_sessions SET ticket = ? WHERE token = ?", (ticket, token))
        return web.json_response({"claim": self.claim(ticket)}, headers={"Cache-Control": "no-store"})

    def routes(self, app):
        app.router.add_get("/tree", self.page)
        app.router.add_get("/tree/board", self.page)
        app.router.add_post("/tree/session", self.start)
        app.router.add_post("/tree/win", self.win)
