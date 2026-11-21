"""Shoot a barev HTTP routes and server-side scoring."""
import math
import re
import secrets
import time
from contextlib import closing
from pathlib import Path

from aiohttp import web

GAME_TEMPLATE = Path(__file__).with_name("game.html")
SESSION_AGE = 3600
GRAVITY = 1900
HOOP_Y = 210
BALL_STARTS = ((50, 655), (110, 655), (170, 655), (230, 655), (290, 655), (140, 601), (200, 601))
TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{32}")


def target():
    return 95 + secrets.randbelow(151)


def hits_hoop(vx, vy, hoop, ball=2):
    x, y = BALL_STARTS[ball]
    discriminant = vy * vy - 2 * GRAVITY * (y - HOOP_Y)
    if discriminant <= 0:
        return False
    descent = (-vy + math.sqrt(discriminant)) / GRAVITY
    return abs(x + vx * descent - hoop) <= 18


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
            "score": row["score"], "hoop": row["hoop"], "turn": row["turn"],
            "hit": bool(row["hit"]), "claim": claim, "balls": BALL_STARTS,
        }, headers={"Cache-Control": "no-store"})

    @staticmethod
    def session(db, request, now):
        token = request.cookies.get("barev_game", "")
        if not TOKEN_FORMAT.fullmatch(token):
            return None
        return db.execute(
            "SELECT * FROM game_sessions WHERE token = ? AND expires > ?", (token, now)
        ).fetchone()

    async def page(self, request):
        return web.FileResponse(GAME_TEMPLATE)

    async def dalmatian(self, request):
        return web.FileResponse(GAME_TEMPLATE.with_name("dalmatian.png"))

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
                (token, now + SESSION_AGE, target()),
            )
            response = self.state(self.session_for_token(db, token))
        response.set_cookie("barev_game", token, max_age=SESSION_AGE,
                            secure=True, httponly=True, samesite="Strict", path="/game")
        return response

    @staticmethod
    def session_for_token(db, token):
        return db.execute("SELECT * FROM game_sessions WHERE token = ?", (token,)).fetchone()

    async def shoot(self, request):
        if request.content_length is None or request.content_length > 256:
            raise web.HTTPBadRequest()
        try:
            data = await request.json()
            turn, vx, vy = (data[key] for key in ("turn", "vx", "vy"))
            ball = data.get("ball", 2)
            if type(ball) is not int or not 0 <= ball < len(BALL_STARTS):
                raise ValueError
            if type(turn) is not int or type(vx) not in (int, float) or type(vy) not in (int, float):
                raise ValueError
            if not (-240 <= vx <= 240 and -1750 <= vy <= -400):
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            raise web.HTTPBadRequest() from None

        now = time.time()
        with closing(self.rewards.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self.session(db, request, now)
            if not row:
                raise web.HTTPUnauthorized()
            if turn != row["turn"] or row["ticket"] or now < row["next_shot"]:
                return self.state(row)

            hit = hits_hoop(vx, vy, row["hoop"], ball)
            score = row["score"] + int(hit)
            ticket = secrets.token_urlsafe(24) if score == 5 else None
            if ticket:
                db.execute("INSERT INTO game_wins (ticket) VALUES (?)", (ticket,))
            db.execute(
                "UPDATE game_sessions SET turn = turn + 1, score = ?, hit = ?, "
                "hoop = ?, ticket = ?, next_shot = ? WHERE token = ?",
                (score, hit, target(), ticket, now + .85, row["token"]),
            )
            return self.state(self.session_for_token(db, row["token"]))

    def routes(self, app):
        app.router.add_get("/game", self.page)
        app.router.add_get("/game/dalmatian.png", self.dalmatian)
        app.router.add_post("/game/session", self.start)
        app.router.add_post("/game/shot", self.shoot)
