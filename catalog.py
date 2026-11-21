"""Catalog + orders module. Products are added by an admin from Telegram:
photo with caption  Name | price | sizes, comma | colors, comma | note
Orders from the page/Mini App go to every admin chat."""
import io, json, uuid
from pathlib import Path

from PIL import Image
from aiohttp import web
from aiogram.types import FSInputFile, Message

HELP = ("Формат подписи к фото:\nНазвание | цена | размеры через запятую | цвета через запятую | примечание\n"
        "Например: Hoodie | 15000 | S,M,L | Blue,Lilac | Notebook as a gift\nПустые поля можно пропустить.")


class Catalog:
    def __init__(self, data: Path, bot, admin_chats):
        self.dir = data / "products"; self.dir.mkdir(parents=True, exist_ok=True)
        self.db = data / "products.json"; self.bot = bot; self.admin_chats = admin_chats

    def load(self):
        return json.loads(self.db.read_text()) if self.db.exists() else []

    def save(self, items):
        self.db.write_text(json.dumps(items, ensure_ascii=False))

    @staticmethod
    def parse(caption):
        parts = [x.strip() for x in caption.split("|")]
        price = parts[1].replace(" ", "") if len(parts) > 1 else ""
        if not parts[0] or not price.isdigit():
            return None
        split = lambda i: [x.strip() for x in parts[i].split(",") if x.strip()] if len(parts) > i else []
        return {"name": parts[0], "price": int(price), "sizes": split(2), "colors": split(3),
                "note": parts[4] if len(parts) > 4 else ""}

    # ---- telegram (admin) ----
    async def add(self, m: Message, buf):
        p = self.parse(m.caption or "")
        if not p:
            return await m.answer(HELP)
        pid = uuid.uuid4().hex[:6]
        im = Image.open(buf).convert("RGB"); im.thumbnail((1200, 1500)); im.save(self.dir / f"{pid}.jpg", quality=86)
        p["id"] = pid; items = self.load(); items.append(p); self.save(items)
        await m.answer(f"В каталоге: {p['name']}, {p['price']} AMD\nРазмеры: {', '.join(p['sizes']) or '-'}\n"
                       f"Цвета: {', '.join(p['colors']) or '-'}\nУдалить: /pdel {pid}")

    async def listing(self, m: Message):
        items = self.load()
        await m.answer("\n".join(f"{p['id']}  {p['name']}  {p['price']} AMD" for p in items) or "Каталог пуст.\n\n" + HELP)

    async def delete(self, m: Message):
        pid = (m.text or "").split(maxsplit=1)[1].strip() if len((m.text or "").split()) > 1 else ""
        items = self.load(); left = [p for p in items if p["id"] != pid]
        if len(left) == len(items):
            return await m.answer("Не найдено. Список: /products")
        self.save(left); (self.dir / f"{pid}.jpg").unlink(missing_ok=True)
        await m.answer(f"Удалено: {pid}")

    # ---- web ----
    async def api(self, _):
        return web.json_response(self.load())

    async def photo(self, req):
        f = self.dir / (req.match_info["pid"] + ".jpg")
        return web.FileResponse(f) if f.exists() else web.Response(status=404)

    async def order(self, req):
        try:
            d = await req.json()
        except Exception:
            return web.json_response({"ok": False}, status=400)
        p = {x["id"]: x for x in self.load()}.get(str(d.get("id", "")))
        name = str(d.get("name", "")).strip()[:60]; phone = str(d.get("phone", "")).strip()[:30]
        if not p or not name or sum(c.isdigit() for c in phone) < 6:
            return web.json_response({"ok": False}, status=400)
        text = (f"🛍 Новый заказ\n{p['name']} · {p['price']} AMD\nЦвет: {str(d.get('color') or '-')[:30]}\n"
                f"Размер: {str(d.get('size') or '-')[:10]}\nИмя: {name}\nТелефон: {phone}\n"
                f"Комментарий: {str(d.get('note') or '-')[:300]}")
        sent = 0
        for cid in self.admin_chats():
            try:
                await self.bot.send_photo(cid, FSInputFile(self.dir / f"{p['id']}.jpg"), caption=text); sent += 1
            except Exception:
                pass
        return web.json_response({"ok": sent > 0})

    def routes(self, app):
        app.router.add_get("/api/products", self.api)
        app.router.add_get("/products/{pid}.jpg", self.photo)
        app.router.add_post("/api/order", self.order)
