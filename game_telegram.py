"""Telegram entry points for claiming and redeeming game rewards."""
from contextlib import closing
import io
import logging

import qrcode
from aiogram import F
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
)


def keyboard(label, action):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=action)]
    ])


def register_reward_handlers(router, rewards, bot, admin_chats, is_admin, bot_username):

    async def request_contact(message, ticket, telegram_id):
        if not rewards.begin_claim(ticket, telegram_id):
            return await message.answer("Эта победа недоступна.")
        await message.answer(
            "Чтобы получить открытку, поделись своим номером телефона. "
            "Нажимая кнопку ниже и подтверждая отправку контакта в Telegram, ты соглашаешься "
            "на сохранение номера, Telegram ID и ника (если он указан) для учёта и выдачи приза.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="Поделиться контактом и получить QR", request_contact=True)]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )

    @router.message(CommandStart(deep_link=True), F.text.regexp(r"^/start(?:@\w+)? win_[\w-]{32}$"))
    async def offer(message):
        if message.chat.type != "private":
            return
        ticket = message.text.split()[1][4:]
        await request_contact(message, ticket, message.from_user.id)

    @router.callback_query(F.data.regexp(r"^claim:[\w-]{32}$"))
    async def request_from_old_button(callback):
        if callback.message.chat.type != "private":
            return await callback.answer()
        ticket = callback.data.split(":", 1)[1]
        await callback.answer()
        await request_contact(callback.message, ticket, callback.from_user.id)

    @router.message(F.contact, F.chat.type == "private")
    async def claim(message):
        user = message.from_user
        contact = message.contact
        if contact.user_id != user.id:
            return await message.answer("Поделись своим контактом с помощью кнопки ниже.")
        reward, created = rewards.claim_after_consent(user.id, user.username, contact.phone_number)
        if reward is None:
            return await message.answer(
                "Открой ссылку на получение приза из игры ещё раз.",
                reply_markup=ReplyKeyboardRemove(),
            )
        if reward["redeemed_at"]:
            return await message.answer("Открытка уже выдана.", reply_markup=ReplyKeyboardRemove())
        code = reward["code"]
        if created:
            for chat_id in admin_chats():
                try:
                    await bot.send_message(chat_id, f"Оформлен приз: бумажная открытка. Код {code}.")
                except Exception:
                    logging.exception("Could not notify postcard reward admin")
        qr = qrcode.make(f"https://t.me/{bot_username()}?start=gift_{code}")
        image = io.BytesIO()
        qr.save(image, format="PNG")
        await message.answer_photo(
            BufferedInputFile(image.getvalue(), filename="postcard-prize.png"),
            caption=(
                "Покажи этот QR, чтобы получить бумажную открытку в подарок. "
                "BAREV JANIK, Ереван, ул. Пап Тагабор, 8, во дворе, с 12:00 до 20:00."
            ),
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.message(CommandStart(deep_link=True), F.text.regexp(r"^/start(?:@\w+)? gift_[A-F0-9]{24}$"))
    async def inspect(message):
        if message.chat.type != "private" or not is_admin(message):
            return await message.answer("Покажи QR сотруднику магазина.")
        code = message.text.split()[1][5:]
        with closing(rewards.connect()) as db:
            row = db.execute(
                "SELECT redeemed_at FROM postcard_rewards WHERE code = ?", (code,)
            ).fetchone()
        if row is None:
            return await message.answer("Приз не найден.")
        if row["redeemed_at"]:
            return await message.answer("Открытка уже выдана.")
        await message.answer("Можно выдать одну открытку.", reply_markup=keyboard("Списать", f"redeem:{code}"))

    @router.callback_query(F.data.regexp(r"^redeem:[A-F0-9]{24}$"))
    async def redeem(callback):
        if callback.message.chat.type != "private" or not is_admin(callback):
            return await callback.answer("Только для администратора.", show_alert=True)
        code = callback.data.split(":", 1)[1]
        result = rewards.redeem(code, callback.from_user.id)
        status = {"redeemed": "Открытка выдана.", "already_redeemed": "Открытка уже выдана.", "unknown": "Приз не найден."}
        await callback.answer()
        await callback.message.edit_text(status[result])

    return router
