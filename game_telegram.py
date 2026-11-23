"""Telegram entry points for claiming and redeeming game rewards."""
import asyncio
from contextlib import suppress
import io
import logging

import qrcode
from aiogram import F
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
)


COPY = {
    "consent": {
        "ru": "🎁 Получи свою открытку\n\nНажми кнопку ниже и подтверди передачу номера телефона.\nBAREV, JANIK сохранит твой номер, Telegram ID и ник, если он есть, для выдачи приза.\n\nНа один номер — один QR-код и одна открытка.",
        "en": "🎁 Claim your postcard\n\nTap below to share your phone number.\nBAREV, JANIK will store your phone number, Telegram ID and username, if available, to fulfil your prize claim.\n\nOne phone number — one QR code and one postcard.",
        "hy": "🎁 Ստացիր քո բացիկը\n\nՍեղմիր ներքևի կոճակը և հաստատիր հեռախոսահամարի փոխանցումը։\nBAREV, JANIK-ը կպահպանի քո հեռախոսահամարը, Telegram ID-ն և օգտանունը՝ մրցանակը տրամադրելու համար։\n\nՄեկ հեռախոսահամար — մեկ QR կոդ և մեկ բացիկ։",
    },
    "marketing_yes": {"ru": "📣 Ты выбрал(а) новости, акции и подарки в Telegram. Отказаться можно командой /stop. Это не влияет на получение открытки.", "en": "📣 You opted in to news, promotions and gift offers in Telegram. Unsubscribe with /stop. This does not affect your postcard.", "hy": "📣 Դու ընտրել ես նորություններ, ակցիաներ և նվերների առաջարկներ Telegram-ում։ Հրաժարվելու համար ուղարկիր /stop։ Սա չի ազդում բացիկը ստանալու վրա։"},
    "marketing_no": {"ru": "Рассылка не выбрана. Открытку всё равно можно получить.", "en": "You have not opted in to marketing. You can still collect your postcard.", "hy": "Դու չես ընտրել գովազդային հաղորդագրությունները։ Դու միևնույն է կարող ես ստանալ բացիկը։"},
    "marketing_stopped": {"ru": "Рассылка отключена. Право на приз и твой QR-код сохраняются.", "en": "Marketing messages are disabled. Your prize and QR code remain valid.", "hy": "Գովազդային հաղորդագրություններն անջատված են։ Քո մրցանակը և QR կոդը մնում են ուժի մեջ։"},
    "contact": {"ru": "📱 Поделиться номером и получить QR", "en": "📱 Share contact and get QR code", "hy": "📱 Ուղարկել համարը և ստանալ QR կոդը"},
    "unavailable": {"ru": "Ссылка на приз недоступна. Открой её из игры ещё раз.", "en": "This prize claim is unavailable. Open the claim link from the game again.", "hy": "Մրցանակի հղումն անհասանելի է։ Նորից բացիր այն խաղից։"},
    "own_contact": {"ru": "Нажми кнопку ниже, чтобы передать свой номер.", "en": "Use the button below to share your own contact.", "hy": "Սեղմիր ներքևի կոճակը՝ քո համարը փոխանցելու համար։"},
    "missing": {"ru": "Открой ссылку на приз из игры ещё раз.", "en": "Open the prize claim link from the game again.", "hy": "Նորից բացիր մրցանակի հղումը խաղից։"},
    "collected": {"ru": "Твоя открытка уже выдана. На один номер — одна открытка.", "en": "Your postcard has already been collected. One postcard per phone number.", "hy": "Քո բացիկն արդեն ստացված է։ Մեկ հեռախոսահամարին՝ մեկ բացիկ։"},
    "caption": {
        "ru": "🎁 Твоя бумажная открытка ждёт тебя!\n\n📍 BAREV, JANIK\nул. Пап Тагабор, 8, Ереван, во дворе\n\n🕛 Ждём тебя с 12:00 до 20:00\n\n🎟 Покажи этот QR-код сотруднику при получении.\nОдин номер телефона — одна открытка.",
        "en": "🎁 Your paper postcard is waiting for you!\n\n📍 BAREV, JANIK\n8 Pap Tagavor St., Yerevan, in the courtyard\n\n🕛 12:00–20:00\n\n🎟 Show this QR code to the staff to collect your postcard.\nOne phone number — one postcard.",
        "hy": "🎁 Քո թղթե բացիկը սպասում է քեզ։\n\n📍 BAREV, JANIK\nՊապ Թագավորի փ. 8, Երևան, բակում\n\n🕛 12:00–20:00\n\n🎟 Բացիկը ստանալու համար այս QR կոդը ցույց տուր աշխատակցին։\nՄեկ հեռախոսահամարին՝ մեկ բացիկ։",
    },
    "existing": {"ru": "🎟 Приз уже оформлен. Повторный QR-код не выдаётся.\n\nИграй дальше сколько хочешь и отправляй электронные открытки друзьям!", "en": "🎟 This prize has already been claimed. No second QR code is issued.\n\nKeep playing and send digital postcards to your friends!", "hy": "🎟 Մրցանակն արդեն ձևակերպված է։ Կրկնակի QR կոդ չի տրամադրվում։\n\nՇարունակիր խաղալ և էլեկտրոնային բացիկներ ուղարկել ընկերներիդ։"},
}


def player_text(user, key):
    language = (getattr(user, "language_code", None) or "en").split("-")[0]
    return COPY[key].get(language, COPY[key]["en"])


def keyboard(label, action):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=action)]
    ])


def register_reward_handlers(router, rewards, bot, admin_chats, is_admin, bot_username):

    notification_lock = asyncio.Lock()
    notification_task = None

    async def notify_pending():
        async with notification_lock:
            for admin_id in admin_chats():
                for reward in rewards.pending_notifications(admin_id):
                    code = reward["code"]
                    username = "@" + reward["telegram_username"] if reward["telegram_username"] else "Ник не указан"
                    try:
                        await bot.send_message(
                            admin_id,
                            "🎁 Оформлен приз — одна бумажная открытка\n\n"
                            f"👤 {username}\n"
                            f"📱 {reward['phone_number'] or 'Номер не указан'}\n"
                            f"🆔 Telegram ID {reward['telegram_id']}\n"
                            f"🎟 Код {code}\n\n"
                            f"📣 Рассылка: {'согласие получено' if rewards.marketing_enabled(reward['telegram_id']) else 'не подписан(а)'}\n\n"
                            "Проверь код при выдаче открытки.",
                            reply_markup=keyboard("Открыть приз", f"inspect:{code}"),
                        )
                    except TelegramAPIError as error:
                        logging.warning("Postcard admin notification failed (%s)", type(error).__name__)
                        break
                    rewards.mark_notified(code, admin_id)

    async def notification_worker():
        while True:
            try:
                await notify_pending()
            except Exception as error:
                logging.warning("Postcard notification retry failed (%s)", type(error).__name__)
            await asyncio.sleep(60)

    async def start_notifications():
        nonlocal notification_task
        notification_task = asyncio.create_task(notification_worker())

    async def stop_notifications():
        if notification_task:
            notification_task.cancel()
            with suppress(asyncio.CancelledError):
                await notification_task

    router.startup.register(start_notifications)
    router.shutdown.register(stop_notifications)

    async def show_prizes(message, offset=0):
        entries = rewards.list_rewards()
        await message.answer(
            "🎁 Призы и контакты\n\n"
            f"Всего {len(entries)} · ожидают получения {sum(not row['redeemed_at'] for row in entries)}\n"
            "На один номер телефона — один код и одна открытка."
        )
        for reward in entries[offset:offset + 10]:
            username = "@" + reward["telegram_username"] if reward["telegram_username"] else "Ник не указан"
            status = "Открытка выдана" if reward["redeemed_at"] else "Ожидает получения"
            await message.answer(
                f"👤 {username}\n📱 {reward['phone_number'] or 'Номер не указан'}\n"
                f"🆔 Telegram ID {reward['telegram_id']}\n🎟 {reward['code']}\n{status}\n"
                f"📣 Рассылка: {'согласие получено' if rewards.marketing_enabled(reward['telegram_id']) else 'не подписан(а)'}",
                reply_markup=keyboard("Проверить приз", f"inspect:{reward['code']}"),
            )
        if len(entries) > offset + 10:
            await message.answer("Другие призы", reply_markup=keyboard("Показать ещё", f"admin:prizes:{offset + 10}"))

    @router.message(Command("prizes"), F.chat.type == "private")
    async def prize_list(message):
        if is_admin(message):
            await show_prizes(message)

    @router.callback_query(F.data.regexp(r"^admin:prizes(?::[0-9]{1,9})?$"))
    async def prize_button(callback):
        if callback.message.chat.type != "private" or not is_admin(callback):
            return await callback.answer("Только для администратора.", show_alert=True)
        await callback.answer()
        offset = int(callback.data.split(":")[2]) if callback.data.count(":") == 2 else 0
        await show_prizes(callback.message, offset)

    async def request_contact(message, ticket, user, marketing_opt_in=False):
        telegram_id = user.id
        previous = rewards.reward_for_telegram(telegram_id)
        if previous and (previous["qr_sent_at"] or previous["redeemed_at"] or previous["telegram_id"] != telegram_id):
            return await message.answer(
                player_text(user, "collected" if previous["redeemed_at"] else "existing"),
                reply_markup=ReplyKeyboardRemove(),
            )
        if not rewards.begin_claim(ticket, telegram_id, marketing_opt_in):
            return await message.answer(player_text(user, "unavailable"))
        await message.answer(
            player_text(user, "consent") + "\n\n" + player_text(user, "marketing_yes" if marketing_opt_in else "marketing_no"),
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text=player_text(user, "contact"), request_contact=True)]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )

    @router.message(CommandStart(deep_link=True), F.text.regexp(r"^/start(?:@\w+)? win_[\w-]{32}(?:_m[01])?$"))
    async def offer(message):
        if message.chat.type != "private":
            return
        payload = message.text.split()[1][4:]
        ticket = payload[:32]
        await request_contact(message, ticket, message.from_user, payload[32:] == "_m1")

    @router.callback_query(F.data.regexp(r"^claim:[\w-]{32}$"))
    async def request_from_old_button(callback):
        if callback.message.chat.type != "private":
            return await callback.answer()
        ticket = callback.data.split(":", 1)[1]
        await callback.answer()
        await request_contact(callback.message, ticket, callback.from_user)

    @router.message(Command("stop"), F.chat.type == "private")
    async def stop_marketing(message):
        rewards.stop_marketing(message.from_user.id)
        await message.answer(player_text(message.from_user, "marketing_stopped"))

    @router.message(F.contact, F.chat.type == "private")
    async def claim(message):
        user = message.from_user
        contact = message.contact
        if contact.user_id != user.id:
            return await message.answer(player_text(user, "own_contact"))
        try:
            reward, created = rewards.claim_after_consent(user.id, user.username, contact.phone_number)
        except ValueError:
            return await message.answer("Не удалось прочитать номер. Нажми кнопку передачи контакта ещё раз.")
        if reward is None:
            return await message.answer(
                player_text(user, "missing"),
                reply_markup=ReplyKeyboardRemove(),
            )
        if reward["redeemed_at"]:
            return await message.answer(player_text(user, "collected"), reply_markup=ReplyKeyboardRemove())
        if not created and (reward["qr_sent_at"] or reward["telegram_id"] != user.id):
            return await message.answer(player_text(user, "existing"), reply_markup=ReplyKeyboardRemove())
        code = reward["code"]
        qr = qrcode.make(f"https://t.me/{bot_username()}?start=gift_{code}")
        image = io.BytesIO()
        qr.save(image, format="PNG")
        await message.answer_photo(
            BufferedInputFile(image.getvalue(), filename="postcard-prize.png"),
            caption=player_text(user, "caption"),
            reply_markup=ReplyKeyboardRemove(),
        )

        rewards.mark_qr_sent(code)
        await notify_pending()

    async def inspect_prize(message, code):
        reward = rewards.reward_by_code(code)
        if reward is None:
            return await message.answer("Приз не найден.")
        if reward["redeemed_at"]:
            return await message.answer("Открытка уже выдана.")
        await message.answer("🎁 Можно выдать одну открытку.", reply_markup=keyboard("Списать", f"redeem:{reward['code']}"))

    @router.callback_query(F.data.regexp(r"^inspect:[A-F0-9]{24}$"))
    async def inspect_button(callback):
        if callback.message.chat.type != "private" or not is_admin(callback):
            return await callback.answer("Только для администратора.", show_alert=True)
        await callback.answer()
        await inspect_prize(callback.message, callback.data.split(":", 1)[1])

    @router.message(CommandStart(deep_link=True), F.text.regexp(r"^/start(?:@\w+)? gift_[A-F0-9]{24}$"))
    async def inspect(message):
        if message.chat.type != "private" or not is_admin(message):
            return await message.answer("Show this QR code to a member of staff at the shop.")
        code = message.text.split()[1][5:]
        await inspect_prize(message, code)

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
