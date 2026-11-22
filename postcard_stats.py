"""Count postcard activity for the bot's private admin report."""
from contextlib import closing
from html import escape
import re
import sqlite3

from aiohttp import web
from aiogram import F
from aiogram.filters import Command
from aiogram.types import ChosenInlineResult, LinkPreviewOptions, Message, Update


WEB_METHODS = {"web_share", "web_download"}
METHODS = ("telegram", "web_share", "web_download")
CARD_ID = re.compile(r"[a-z0-9_-]{1,64}")
WEB_EVENT_ID = re.compile(r"[a-f0-9]{32}")
CARD_TITLES = {
    "plain_blue": "Синяя",
    "plain_lilac": "Сиреневая",
    "plain_green": "Зелёная",
    "phone_pink": "Трубка на розовом",
    "phone_green": "Трубка на зелёном",
    "rotary": "Дисковый телефон",
    "balls": "Мячи",
    "dalmatians": "Далматинцы",
    "metro": "Метро",
    "living": "Гостиная",
    "printer": "Принтер",
    "made_in_yerevan": "Made in Yerevan",
    "drip": "Капельница",
}


class PostcardStats:
    def __init__(self, path, list_cards, domain):
        self.path = path
        self.list_cards = list_cards
        self.domain = domain.rstrip("/")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS postcard_events (
                    event_id TEXT PRIMARY KEY,
                    card_id TEXT NOT NULL,
                    method TEXT NOT NULL
                )
            """)

    def record(self, event_id, card_id, method):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT INTO postcard_events (event_id, card_id, method) VALUES (?, ?, ?) "
                "ON CONFLICT (event_id) DO NOTHING",
                (event_id, card_id, method),
            )

    async def web_event(self, request):
        if request.content_type != "application/json" or not 0 < (request.content_length or 0) <= 512:
            raise web.HTTPBadRequest()
        try:
            body = await request.json()
            event_id, card_id, method = (body[key] for key in ("id", "card", "method"))
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest() from None
        if (
            not all(isinstance(value, str) for value in (event_id, card_id, method))
            or not WEB_EVENT_ID.fullmatch(event_id)
            or not CARD_ID.fullmatch(card_id)
            or method not in WEB_METHODS
            or card_id not in self.list_cards()
        ):
            raise web.HTTPBadRequest()
        self.record(f"web:{event_id}", card_id, method)
        return web.Response(status=204)

    def report(self):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT card_id, method, COUNT(*) FROM postcard_events GROUP BY card_id, method"
            ).fetchall()
        totals = dict.fromkeys(METHODS, 0)
        cards = {}
        for card_id, method, count in rows:
            cards.setdefault(card_id, {})[method] = count
            totals[method] += count

        header = (
            "<b>Статистика открыток</b>\n"
            f"Отправлено из бота в Telegram · {totals['telegram']}\n"
            f"Действия «Поделиться» на сайте · {totals['web_share']}\n"
            f"Скачивания с сайта · {totals['web_download']}"
        )
        if not cards:
            return [header + "\n\nПока нет зарегистрированных действий."]

        available = set(self.list_cards())
        lines = []
        for card_id in sorted(cards, key=lambda card: (-sum(cards[card].values()), card)):
            title = escape(CARD_TITLES.get(card_id, card_id))
            if self.domain and card_id in available:
                url = escape(f"{self.domain}/cards/{card_id}.jpg", quote=True)
                title = f'<a href="{url}">{title}</a>'
            counts = " / ".join(str(cards[card_id].get(method, 0)) for method in METHODS)
            lines.append(f"{title} · {counts}")
        return [
            (header + "\n\n" if offset == 0 else "")
            + "<b>По открыткам</b>\nTelegram / «Поделиться» / скачивания\n"
            + "\n".join(lines[offset:offset + 15])
            for offset in range(0, len(lines), 15)
        ]

    def register_telegram(self, router, is_admin):
        @router.chosen_inline_result()
        async def chosen(result: ChosenInlineResult, event_update: Update):
            if CARD_ID.fullmatch(result.result_id):
                self.record(f"telegram:{event_update.update_id}", result.result_id, "telegram")

        @router.message(Command("stats"), F.chat.type == "private")
        async def stats(message: Message):
            if is_admin(message):
                for part in self.report():
                    await message.answer(part, parse_mode="HTML",
                                         link_preview_options=LinkPreviewOptions(is_disabled=True))
