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
BALL_STARTS = ((50, 655), (110, 655), (170, 655), (230, 655), (290, 655), (140, 601), (200, 601))
TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{32}")


def target():
    return 95 + secrets.randbelow(151)


STEP = 1 / 120
GRAVITY = 1900
BOUNCE = .55
HOOP_Y = 245
HOOP_HALF = 36
RIM_RADIUS = 5
BALL_RADIUS = 23


def hoop_x(phase):
    return 170 + 90 * math.sin(phase)


def trace_shot(x, y, vx, vy, phase):
    """Replay one flick using the same court geometry as the browser."""
    armed = y > HOOP_Y + 40 and vy < -200
    hit = False
    frames = []
    for _ in range(300):
        phase = (phase + .7 * STEP) % math.tau
        hx = hoop_x(phase)
        previous_y = y
        vy += GRAVITY * STEP
        x += vx * STEP
        y += vy * STEP
        if x < BALL_RADIUS:
            x, vx = BALL_RADIUS, abs(vx) * BOUNCE
        if x > 340 - BALL_RADIUS:
            x, vx = 340 - BALL_RADIUS, -abs(vx) * BOUNCE
        if y < BALL_RADIUS and vy < 0:
            y, vy = BALL_RADIUS, abs(vy) * BOUNCE
        scored = armed and previous_y < HOOP_Y <= y and vy > 0 and abs(x - hx) <= HOOP_HALF - RIM_RADIUS
        if scored:
            hit = True
            armed = False
        if not (scored or (vy > 0 and abs(x - hx) < 26)):
            for rx in (hx - HOOP_HALF, hx + HOOP_HALF):
                dx, dy = x - rx, y - HOOP_Y
                distance = math.hypot(dx, dy)
                radius = BALL_RADIUS + RIM_RADIUS
                if 0 < distance < radius:
                    nx, ny = dx / distance, dy / distance
                    x, y = rx + nx * radius, HOOP_Y + ny * radius
                    velocity = vx * nx + vy * ny
                    if velocity < 0:
                        vx -= 1.6 * velocity * nx
                        vy -= 1.6 * velocity * ny
        y = min(y, 655)
        frames.append([round(x, 1), round(y, 1), round(hx, 1), scored])
        if y == 655:
            break
    return hit, frames

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

    def state(self, row, frames=None):
        claim = None
        if row["ticket"]:
            claim = f"https://t.me/{self.bot_username()}?start=win_{row['ticket']}"
        return web.json_response({
            "score": row["score"], "hoop": row["hoop"], "turn": row["turn"],
            "hit": bool(row["hit"]), "claim": claim, "balls": BALL_STARTS, "frames": frames,
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
        if request.content_length is None or request.content_length > 512:
            raise web.HTTPBadRequest()
        try:
            data = await request.json()
            turn = data["turn"]
            x, y, vx, vy, phase = (data[key] for key in ("x", "y", "vx", "vy", "phase"))
            if type(turn) is not int or any(type(value) not in (int, float) for value in (x, y, vx, vy, phase)):
                raise ValueError
            if not (23 <= x <= 317 and 23 <= y <= 655 and -1800 <= vx <= 1800
                    and -2300 <= vy <= 2300 and 0 <= phase <= math.tau):
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

            hit, frames = trace_shot(x, y, vx, vy, phase)
            score = row["score"] + int(hit)
            ticket = secrets.token_urlsafe(24) if score == 5 else None
            if ticket:
                db.execute("INSERT INTO game_wins (ticket) VALUES (?)", (ticket,))
            db.execute(
                "UPDATE game_sessions SET turn = turn + 1, score = ?, hit = ?, "
                "hoop = ?, ticket = ?, next_shot = ? WHERE token = ?",
                (score, hit, target(), ticket, now + .85, row["token"]),
            )
            return self.state(self.session_for_token(db, row["token"]), frames)

    def routes(self, app):
        app.router.add_get("/game", self.page)
        app.router.add_post("/game/session", self.start)
        app.router.add_post("/game/shot", self.shoot)
