"""Shoot a barev HTTP routes and server-side scoring."""
import re
import secrets
import time
from contextlib import closing
from pathlib import Path

from aiohttp import web

GAME_TEMPLATE = Path(__file__).with_name("game.html")
SESSION_AGE = 3600
HOOP_Y = 245
RIM_CLEARANCE = 36 - 5
COOKIE_NAME = "barev_original"
TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{32}")


def valid_crossing(x, previous_y, y, vy, hoop_x):
    return (
        0 <= x <= 340 and 80 <= hoop_x <= 260
        and HOOP_Y - 70 <= previous_y < HOOP_Y <= y <= HOOP_Y + 70
        and 0 < vy <= 4000 and abs(x - hoop_x) < RIM_CLEARANCE
    )


class Game:
    def __init__(self, rewards, bot_username):
        self.rewards = rewards
        self.bot_username = bot_username
        with closing(rewards.connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS game_sessions (
                    token TEXT PRIMARY KEY,
                    expires REAL NOT NULL,
                    hoop INTEGER NOT NULL,
                    turn INTEGER NOT NULL DEFAULT 0,
                    score INTEGER NOT NULL DEFAULT 0,
                    hit INTEGER NOT NULL DEFAULT 0,
                    next_shot REAL NOT NULL DEFAULT 0,
                    ticket TEXT
                )
            """)

    def state(self, row):
        claim = None
        if row["ticket"]:
            claim = f"https://t.me/{self.bot_username()}?start=win_{row['ticket']}"
        return web.json_response({
            "score": row["score"], "turn": row["turn"], "claim": claim,
        }, headers={"Cache-Control": "no-store"})

    @staticmethod
    def session(db, request, now):
        token = request.cookies.get(COOKIE_NAME, "")
        if not TOKEN_FORMAT.fullmatch(token):
            return None
        return db.execute(
            "SELECT * FROM game_sessions WHERE token = ? AND expires > ?", (token, now)
        ).fetchone()

    async def page(self, request):
        return web.FileResponse(GAME_TEMPLATE)

    async def start(self, request):
        now = time.time()
        with closing(self.rewards.connect()) as db, db:
            row = self.session(db, request, now)
            if row:
                return self.state(row)
            token = secrets.token_urlsafe(24)
            db.execute("DELETE FROM game_sessions WHERE expires <= ?", (now,))
            db.execute(
                "INSERT INTO game_sessions (token, expires, hoop) VALUES (?, ?, ?)",
                (token, now + SESSION_AGE, 170),
            )
            response = self.state(self.session_for_token(db, token))
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_AGE,
                            secure=True, httponly=True, samesite="Strict", path="/game")
        return response

    @staticmethod
    def session_for_token(db, token):
        return db.execute("SELECT * FROM game_sessions WHERE token = ?", (token,)).fetchone()

    async def hit(self, request):
        if request.content_length is None or request.content_length > 512:
            raise web.HTTPBadRequest()
        try:
            data = await request.json()
            turn = data["turn"]
            crossing = [data[k] for k in ("x", "previousY", "y", "vy", "hoopX")]
            if type(turn) is not int or any(type(value) not in (int, float) for value in crossing):
                raise ValueError
            if not valid_crossing(*crossing):
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            raise web.HTTPBadRequest() from None

        now = time.time()
        with closing(self.rewards.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self.session(db, request, now)
            if not row:
                raise web.HTTPUnauthorized()
            if turn != row["turn"] or row["ticket"]:
                return self.state(row)

            score = row["score"] + 1
            ticket = secrets.token_urlsafe(24) if score == 5 else None
            if ticket:
                db.execute("INSERT INTO game_wins (ticket) VALUES (?)", (ticket,))
            db.execute(
                "UPDATE game_sessions SET turn = turn + 1, score = ?, ticket = ? WHERE token = ?",
                (score, ticket, row["token"]),
            )
            return self.state(self.session_for_token(db, row["token"]))

    def routes(self, app):
        app.router.add_get("/game", self.page)
        app.router.add_get("/game/board", self.page)
        app.router.add_post("/game/session", self.start)
        app.router.add_post("/game/hit", self.hit)
