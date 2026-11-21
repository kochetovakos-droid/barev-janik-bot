from pathlib import Path

from aiohttp import web


GAME_TEMPLATE = Path(__file__).with_name("game.html")


async def show_game(request: web.Request) -> web.FileResponse:
    return web.FileResponse(GAME_TEMPLATE)


def register_game(app: web.Application) -> None:
    app.router.add_get("/game", show_game)
