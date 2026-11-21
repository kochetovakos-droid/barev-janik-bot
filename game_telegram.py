"""Telegram entry points for claiming and redeeming game rewards."""
from contextlib import closing
import io
import logging

import qrcode
from aiogram import F
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup


def keyboard(label, action):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=action)]
    ])


def register_reward_handlers(router, rewards, bot, admin_chats, is_admin, bot_username):

    @router.message(CommandStart(deep_link=True), F.text.regexp(r"^/start(?:@\w+)? win_[\w-]{32}$"))
    async def offer(message):
        if message.chat.type != "private":
            return
        ticket = message.text.split()[1][4:]
        await message.answer(
            "Нажимая «Согласиться и получить QR», ты разрешаешь боту сохранить "
            "твой Telegram ID для учёта и выдачи бумажной открытки.",
            reply_markup=keyboard("Согласиться и получить QR", f"claim:{ticket}"),
        )

    @router.callback_query(F.data.regexp(r"^claim:[\w-]{32}$"))
    async def claim(callback):
        if callback.message.chat.type != "private":
            return await callback.answer()
        ticket = callback.data.split(":", 1)[1]
        reward, created = rewards.claim_after_consent(ticket, callback.from_user.id)
        if reward is None:
            return await callback.answer("Эта победа недоступна.", show_alert=True)
        if reward["redeemed_at"]:
            return await callback.answer("Открытка уже выдана.", show_alert=True)
        await callback.answer()
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
        await callback.message.answer_photo(
            BufferedInputFile(image.getvalue(), filename="postcard-prize.png"),
            caption="Покажи этот QR в магазине BAREV JANIK, чтобы получить открытку.",
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
