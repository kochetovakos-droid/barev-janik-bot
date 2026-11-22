"""BAREV, JANIK: postcard bot + call page. One process: Telegram polling + web server."""
import os, json, asyncio, io, time, uuid, shutil
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           InlineQuery, InlineQueryResultPhoto, InputMediaPhoto, Message, MenuButtonWebApp, WebAppInfo)
from catalog import Catalog
from game import Game
from game_rewards import Rewards
from game_telegram import register_reward_handlers

BASE = Path(__file__).parent
TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {a.strip().lstrip("@").lower() for a in os.environ.get("ADMINS", "").split(",") if a.strip()}
DOMAIN = os.environ.get("PUBLIC_URL") or (("https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]) if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "")
QR_URL = os.environ.get("QR_URL") or DOMAIN or "https://instagram.com/barev.janik"
DATA = Path(os.environ.get("DATA_DIR") or ("/data" if Path("/data").exists() else BASE / "data"))
SRC, OUT, DB = DATA / "src", DATA / "cards", DATA / "cards.json"

W, IH, S = 720, 894, 170
BLUE = (20, 51, 162)
LINES = ["8 Pap Tagavor St, Yerevan", "courtyard · 12:00-20:00", "@barev.janik · made in Yerevan"]
FB = ImageFont.truetype(str(BASE / "Heavy.otf"), 34)
FM = ImageFont.truetype(str(BASE / "Medium.otf"), 21)


def qr_img():
    q = qrcode.QRCode(border=1, box_size=10, error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(QR_URL); q.make()
    return q.make_image(fill_color=BLUE, back_color="white").convert("RGB").resize((128, 128), Image.NEAREST)


def render(src: Path, out: Path, qr):
    """Whole picture, no crop, no margins: fit to 720x894, extend the flat edge colour if proportions differ."""
    im = Image.open(src).convert("RGB")
    r = im.height / im.width
    if abs(r - IH / W) < 0.01:
        im = im.resize((W, IH), Image.LANCZOS)
    elif r > IH / W:  # taller: fit height, extend to the right with the top-right colour
        bg = im.getpixel((im.width - 3, 3)); im = im.resize((round(im.width * IH / im.height), IH), Image.LANCZOS)
        ext = Image.new("RGB", (W, IH), bg); ext.paste(im, (0, 0)); im = ext
    else:  # wider: fit width, extend upward with the top-left colour
        bg = im.getpixel((3, 3)); im = im.resize((W, round(im.height * W / im.width)), Image.LANCZOS)
        ext = Image.new("RGB", (W, IH), bg); ext.paste(im, (0, IH - im.height)); im = ext
    card = Image.new("RGB", (W, IH + S), "white"); card.paste(im, (0, 0))
    d = ImageDraw.Draw(card); y = IH + 22
    card.paste(qr, (32, y)); d.text((184, y - 4), "BAREV, JANIK", font=FB, fill=BLUE)
    for i, line in enumerate(LINES):
        d.text((184, y + 44 + i * 27), line, font=FM, fill=BLUE)
    card.save(out, quality=80)


def load():
    return json.loads(DB.read_text()) if DB.exists() else []


def save(ids):
    DB.write_text(json.dumps(ids))


def setup():
    for p in (SRC, OUT): p.mkdir(parents=True, exist_ok=True)
    if not DB.exists():
        ids = []
        for f in sorted(BASE.glob("[0-9][0-9]_*.jpg")):
            cid = f.stem.split("_", 1)[1]; shutil.copy(f, SRC / f"{cid}.jpg"); ids.append(cid)
        save(ids)
    stamp = DATA / "qr.txt"; fresh = not stamp.exists() or stamp.read_text() != QR_URL
    qr = qr_img()
    for cid in load():
        if fresh or not (OUT / f"{cid}.jpg").exists():
            render(SRC / f"{cid}.jpg", OUT / f"{cid}.jpg", qr)
    stamp.write_text(QR_URL)


VER = str(int(time.time()))
bot = Bot(TOKEN)
dp = Dispatcher()
ME = {"u": ""}


ADMIN_DB = DATA / "admins.json"


def admin_chats():
    return json.loads(ADMIN_DB.read_text()) if ADMIN_DB.exists() else []


def is_admin(m):
    u = m.from_user
    ok = bool(u) and ((u.username or "").lower() in ADMINS or str(u.id) in ADMINS)
    if ok and m.chat.id not in admin_chats():
        ADMIN_DB.parent.mkdir(parents=True, exist_ok=True); ADMIN_DB.write_text(json.dumps(admin_chats() + [m.chat.id]))
    return ok


CAT = Catalog(DATA, bot, admin_chats)


def reward_admin(event):
    user = event.from_user
    return bool(user) and (str(user.id) in ADMINS or (user.username or "").lower() in ADMINS)


DATA.mkdir(parents=True, exist_ok=True)
REWARDS = Rewards(DATA / "game-rewards.sqlite3")
GAME = Game(REWARDS, lambda: ME["u"])
register_reward_handlers(dp, REWARDS, bot, admin_chats, reward_admin, lambda: ME["u"])


def card_kb(i, ids):
    n = len(ids); cid = ids[i]
    rows = [[InlineKeyboardButton(text="◀", callback_data=f"n:{(i - 1) % n}"),
             InlineKeyboardButton(text=f"{i + 1} / {n}", callback_data="noop"),
             InlineKeyboardButton(text="▶", callback_data=f"n:{(i + 1) % n}")],
            [InlineKeyboardButton(text="💌 Send to someone dear", switch_inline_query=cid)]]
    if DOMAIN:
        rows.append([InlineKeyboardButton(text="BAREV, JANIK", url=DOMAIN)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("products"))
async def products(m: Message):
    if is_admin(m):
        await CAT.listing(m)


@dp.message(Command("pdel"))
async def pdel(m: Message):
    if is_admin(m):
        await CAT.delete(m)


@dp.message(CommandStart())
async def start(m: Message):
    is_admin(m)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📞 Answer", callback_data="n:0")]])
    await m.answer("📞 BAREV, JANIK is calling…", reply_markup=kb)


@dp.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()


@dp.callback_query(F.data.startswith("n:"))
async def nav(c: CallbackQuery):
    ids = load()
    if not ids:
        return await c.answer("No postcards yet")
    i = int(c.data[2:]) % len(ids); photo = FSInputFile(OUT / f"{ids[i]}.jpg")
    if c.message.photo:
        await c.message.edit_media(InputMediaPhoto(media=photo), reply_markup=card_kb(i, ids))
    else:
        await c.message.edit_text("Barev, janik! 💛\n\"Hello, dear\" in Armenian.\nPick a postcard and send it to someone dear.")
        await c.message.answer_photo(photo, reply_markup=card_kb(i, ids))
    await c.answer()


@dp.inline_query()
async def inline(q: InlineQuery):
    ids = load(); want = q.query.strip()
    if want in ids:
        ids = [want] + [x for x in ids if x != want]
    res = [InlineQueryResultPhoto(id=cid, photo_url=f"{DOMAIN}/cards/{cid}.jpg?v={VER}",
                                  thumbnail_url=f"{DOMAIN}/cards/{cid}.jpg?v={VER}", photo_width=W, photo_height=IH + S)
           for cid in ids[:50]]
    await q.answer(res, cache_time=30, is_personal=False)


@dp.message(Command("myid"))
async def myid(m: Message):
    await m.answer(f"id: {m.from_user.id}\nusername: @{m.from_user.username}")


@dp.message(Command("list"))
async def lst(m: Message):
    if is_admin(m):
        await m.answer("Postcards (newest first):\n" + "\n".join(load()) + "\n\nDelete: /del name")


@dp.message(Command("del"))
async def delete(m: Message):
    if not is_admin(m):
        return
    cid = (m.text or "").split(maxsplit=1)[1].strip() if len((m.text or "").split()) > 1 else ""
    ids = load()
    if cid not in ids:
        return await m.answer("Not found. See /list")
    ids.remove(cid); save(ids); (OUT / f"{cid}.jpg").unlink(missing_ok=True)
    await m.answer(f"Deleted: {cid}")


@dp.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def add(m: Message):
    if not is_admin(m):
        return
    obj = m.photo[-1] if m.photo else m.document
    buf = io.BytesIO(); await bot.download(obj, destination=buf)
    if (m.caption or "").strip():
        return await CAT.add(m, buf)
    cid = uuid.uuid4().hex[:8]
    Image.open(buf).convert("RGB").save(SRC / f"{cid}.jpg", quality=92)
    render(SRC / f"{cid}.jpg", OUT / f"{cid}.jpg", qr_img())
    ids = load(); ids.insert(0, cid); save(ids)
    await m.answer_photo(FSInputFile(OUT / f"{cid}.jpg"), caption=f"Added: {cid}\nDelete: /del {cid}")


async def page(_):
    return web.FileResponse(BASE / "page.html")


async def ui_language(_):
    return web.FileResponse(BASE / "ui-language.js", headers={"Content-Type": "text/javascript; charset=utf-8"})


async def music(request):
    track = request.match_info["track"]
    return web.FileResponse(BASE / track, headers={"Content-Type": "audio/mpeg"})


async def api(_):
    return web.json_response({"cards": load(), "bot": ME["u"], "v": VER})


async def card(req):
    f = OUT / (req.match_info["cid"] + ".jpg")
    return web.FileResponse(f) if f.exists() else web.Response(status=404)


async def main():
    setup()
    ME["u"] = (await bot.get_me()).username
    app = web.Application()
    app.router.add_get("/", page); app.router.add_get("/api/cards", api); app.router.add_get("/cards/{cid}.jpg", card)
    CAT.routes(app)
    GAME.routes(app)
    app.router.add_get("/game", page)
    app.router.add_get("/ui-language.js", ui_language)
    app.router.add_get("/music/{track:barev-one[.]mp3|barev-two[.]mp3}", music)
    if DOMAIN:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Shoot a barev", web_app=WebAppInfo(url=DOMAIN.rstrip("/") + "/game")))
        except Exception:
            pass
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080))).start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
