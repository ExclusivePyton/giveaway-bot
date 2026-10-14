import asyncio
import logging
import random
import os
from datetime import datetime, timedelta
from typing import List, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import aiosqlite

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN! Добавь переменную окружения на Bothost.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

scheduler = AsyncIOScheduler()

DB_PATH = "giveaways.db"

# ==================== БАЗА ДАННЫХ ====================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_username TEXT,
                title TEXT NOT NULL,
                description TEXT,
                winners_count INTEGER DEFAULT 1,
                end_time TEXT NOT NULL,
                message_id INTEGER,
                required_channels TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                giveaway_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(giveaway_id, user_id)
            )
        """)
        await db.commit()
    logger.info("База данных инициализирована")


async def create_giveaway(creator_id: int, channel_id: int, channel_username: str,
                          title: str, description: str, winners_count: int,
                          end_time: datetime, required_channels: List[int]) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO giveaways 
            (creator_id, channel_id, channel_username, title, description, 
             winners_count, end_time, required_channels)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (creator_id, channel_id, channel_username, title, description,
              winners_count, end_time.isoformat(), ",".join(map(str, required_channels))))
        await db.commit()
        return cursor.lastrowid


async def set_message_id(giveaway_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE giveaways SET message_id = ? WHERE id = ?",
            (message_id, giveaway_id)
        )
        await db.commit()


async def add_participant(giveaway_id: int, user_id: int, username: str, full_name: str) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO participants (giveaway_id, user_id, username, full_name)
                VALUES (?, ?, ?, ?)
            """, (giveaway_id, user_id, username, full_name))
            await db.commit()
            return True
    except aiosqlite.IntegrityError:
        return False


async def get_participants(giveaway_id: int) -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM participants WHERE giveaway_id = ?", (giveaway_id,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_giveaway(giveaway_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM giveaways WHERE id = ?", (giveaway_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_user_giveaways(user_id: int) -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM giveaways WHERE creator_id = ? AND is_active = 1 ORDER BY id DESC",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def finish_giveaway(giveaway_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE giveaways SET is_active = 0 WHERE id = ?", (giveaway_id,))
        await db.commit()


# ==================== FSM ====================

class CreateGiveaway(StatesGroup):
    waiting_channel = State()
    waiting_title = State()
    waiting_description = State()
    waiting_winners = State()
    waiting_duration = State()
    waiting_extra_channels = State()


# ==================== ХЕЛПЕРЫ ====================

async def check_subscription(user_id: int, channel_ids: List[int]) -> tuple[bool, List[int]]:
    """Проверяет подписку. Возвращает (все_ок, список_неподписанных)"""
    not_subscribed = []
    for ch_id in channel_ids:
        try:
            member = await bot.get_chat_member(ch_id, user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                not_subscribed.append(ch_id)
        except TelegramBadRequest:
            not_subscribed.append(ch_id)
    return len(not_subscribed) == 0, not_subscribed


def make_participate_kb(giveaway_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Участвовать", callback_data=f"join:{giveaway_id}")]
    ])


# ==================== КОМАНДЫ ====================

@router.message(Command("start"))
async def cmd_start(message: Message):
    text = (
        "👋 Привет! Я бот для проведения розыгрышей в каналах.\n\n"
        "📋 <b>Как пользоваться:</b>\n"
        "1. Добавь меня администратором в свой канал (с правом публиковать сообщения)\n"
        "2. Напиши /new — создай розыгрыш\n"
        "3. Укажи канал, приз, время и обязательные подписки\n"
        "4. Я опубликую пост с кнопкой «Участвовать»\n\n"
        "<b>Команды:</b>\n"
        "/new — создать розыгрыш\n"
        "/my — мои активные розыгрыши\n"
        "/cancel — отменить создание"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("my"))
async def cmd_my(message: Message):
    giveaways = await get_user_giveaways(message.from_user.id)
    if not giveaways:
        await message.answer("У тебя нет активных розыгрышей.")
        return

    lines = []
    for g in giveaways:
        end = g["end_time"][:16].replace("T", " ")
        lines.append(f"#{g['id']} — <b>{g['title']}</b>\n⏰ до {end} UTC")
    await message.answer("Твои активные розыгрыши:\n\n" + "\n\n".join(lines), parse_mode="HTML")


@router.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext):
    await state.set_state(CreateGiveaway.waiting_channel)
    await message.answer(
        "📢 Перешли мне любое сообщение из канала, в котором хочешь провести розыгрыш.\n"
        "Или отправь @username канала / ID канала.\n\n"
        "⚠️ Бот должен быть администратором канала!"
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Создание розыгрыша отменено.")


@router.message(CreateGiveaway.waiting_channel)
async def process_channel(message: Message, state: FSMContext):
    channel_id = None
    channel_username = None

    if message.forward_from_chat and message.forward_from_chat.type == "channel":
        channel_id = message.forward_from_chat.id
        channel_username = message.forward_from_chat.username
    else:
        text = (message.text or "").strip()
        if text.startswith("@"):
            try:
                chat = await bot.get_chat(text)
                channel_id = chat.id
                channel_username = chat.username
            except Exception:
                await message.answer("❌ Не могу найти такой канал. Проверь @username.")
                return
        elif text.lstrip("-").isdigit():
            channel_id = int(text)
            try:
                chat = await bot.get_chat(channel_id)
                channel_username = chat.username
            except Exception:
                await message.answer("❌ Не могу получить доступ к каналу.")
                return
        else:
            await message.answer("❌ Пришли @username, ID или перешли сообщение из канала.")
            return

    # Проверяем, что бот — админ
    try:
        bot_member = await bot.get_chat_member(channel_id, bot.id)
        if bot_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            await message.answer("❌ Бот должен быть администратором канала!")
            return
    except Exception as e:
        await message.answer(f"❌ Ошибка доступа к каналу: {e}")
        return

    await state.update_data(
        channel_id=channel_id,
        channel_username=channel_username or str(channel_id)
    )
    await state.set_state(CreateGiveaway.waiting_title)
    await message.answer(
        "✅ Канал принят!\n\nТеперь напиши <b>название / приз</b> розыгрыша:",
        parse_mode="HTML"
    )


@router.message(CreateGiveaway.waiting_title)
async def process_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(CreateGiveaway.waiting_description)
    await message.answer("📝 Напиши описание розыгрыша (условия, что разыгрывается и т.д.):")


@router.message(CreateGiveaway.waiting_description)
async def process_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await state.set_state(CreateGiveaway.waiting_winners)
    await message.answer("🏆 Сколько победителей? (число, например 1 или 3):")


@router.message(CreateGiveaway.waiting_winners)
async def process_winners(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit() or int(message.text) < 1:
        await message.answer("Введи целое число ≥ 1")
        return
    await state.update_data(winners_count=int(message.text))
    await state.set_state(CreateGiveaway.waiting_duration)
    await message.answer(
        "⏰ На сколько часов/дней провести розыгрыш?\n"
        "Примеры:\n"
        "• 24 — 24 часа\n"
        "• 3d — 3 дня\n"
        "• 2h — 2 часа"
    )


@router.message(CreateGiveaway.waiting_duration)
async def process_duration(message: Message, state: FSMContext):
    text = (message.text or "").strip().lower()
    try:
        if text.endswith("d"):
            hours = int(text[:-1]) * 24
        elif text.endswith("h"):
            hours = int(text[:-1])
        else:
            hours = int(text)
        if hours < 1:
            raise ValueError
    except ValueError:
        await message.answer("Неверный формат. Примеры: 24, 3d, 12h")
        return

    end_time = datetime.utcnow() + timedelta(hours=hours)
    await state.update_data(end_time=end_time, duration_hours=hours)
    await state.set_state(CreateGiveaway.waiting_extra_channels)
    await message.answer(
        "📢 Хочешь добавить <b>дополнительные каналы</b> для обязательной подписки?\n\n"
        "Отправь @username или ID каналов через пробел/запятую.\n"
        "Или напиши <b>-</b> если только основной канал.",
        parse_mode="HTML"
    )


@router.message(CreateGiveaway.waiting_extra_channels)
async def process_extra_channels(message: Message, state: FSMContext):
    data = await state.get_data()
    required = [data["channel_id"]]

    if (message.text or "").strip() != "-":
        parts = (message.text or "").replace(",", " ").split()
        for part in parts:
            part = part.strip()
            if not part:
                continue
            try:
                if part.startswith("@"):
                    chat = await bot.get_chat(part)
                    ch_id = chat.id
                else:
                    ch_id = int(part)
                # проверяем доступ бота
                await bot.get_chat_member(ch_id, bot.id)
                if ch_id not in required:
                    required.append(ch_id)
            except Exception as e:
                await message.answer(f"⚠️ Не удалось добавить {part}: {e}")

    # Создаём розыгрыш
    giveaway_id = await create_giveaway(
        creator_id=message.from_user.id,
        channel_id=data["channel_id"],
        channel_username=data["channel_username"],
        title=data["title"],
        description=data["description"],
        winners_count=data["winners_count"],
        end_time=data["end_time"],
        required_channels=required
    )

    # Публикуем пост
    text = (
        f"🎁 <b>{data['title']}</b>\n\n"
        f"{data['description']}\n\n"
        f"🏆 Победителей: {data['winners_count']}\n"
        f"⏰ Окончание: {data['end_time'].strftime('%d.%m.%Y %H:%M')} UTC\n\n"
        f"👇 Нажми кнопку, чтобы участвовать"
    )

    try:
        msg = await bot.send_message(
            chat_id=data["channel_id"],
            text=text,
            parse_mode="HTML",
            reply_markup=make_participate_kb(giveaway_id)
        )
        await set_message_id(giveaway_id, msg.message_id)

        # Планируем окончание
        scheduler.add_job(
            end_giveaway_job,
            "date",
            run_date=data["end_time"],
            args=[giveaway_id],
            id=f"giveaway_{giveaway_id}",
            replace_existing=True
        )

        await message.answer(
            f"✅ Розыгрыш создан и опубликован!\n"
            f"ID: <code>{giveaway_id}</code>\n"
            f"Окончание через {data['duration_hours']} ч.",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Не удалось опубликовать пост: {e}")

    await state.clear()


# ==================== УЧАСТИЕ ====================

@router.callback_query(F.data.startswith("join:"))
async def join_giveaway(callback: CallbackQuery):
    giveaway_id = int(callback.data.split(":")[1])
    giveaway = await get_giveaway(giveaway_id)

    if not giveaway or not giveaway["is_active"]:
        await callback.answer("Розыгрыш уже завершён", show_alert=True)
        return

    required = [int(x) for x in (giveaway["required_channels"] or "").split(",") if x]
    ok, not_sub = await check_subscription(callback.from_user.id, required)

    if not ok:
        buttons = []
        for ch_id in not_sub:
            try:
                chat = await bot.get_chat(ch_id)
                if chat.username:
                    link = f"https://t.me/{chat.username}"
                    title = chat.title or chat.username
                    buttons.append([
                        InlineKeyboardButton(
                            text=f"📢 Подписаться на {title}",
                            url=link
                        )
                    ])
            except Exception:
                pass
        buttons.append([
            InlineKeyboardButton(
                text="🔄 Проверить снова",
                callback_data=f"join:{giveaway_id}"
            )
        ])

        await callback.message.answer(
            "❌ Ты не подписан на все необходимые каналы!\n"
            "Подпишись и нажми «Проверить снова».",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
        await callback.answer()
        return

    added = await add_participant(
        giveaway_id,
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.full_name
    )

    if added:
        await callback.answer("✅ Ты успешно участвуешь в розыгрыше!", show_alert=True)
    else:
        await callback.answer("Ты уже участвуешь!", show_alert=True)


# ==================== ЗАВЕРШЕНИЕ ====================

async def end_giveaway_job(giveaway_id: int):
    giveaway = await get_giveaway(giveaway_id)
    if not giveaway or not giveaway["is_active"]:
        return

    participants = await get_participants(giveaway_id)
    await finish_giveaway(giveaway_id)

    if not participants:
        text = (
            f"🎁 Розыгрыш «{giveaway['title']}» завершён.\n\n"
            f"Участников не было 😔"
        )
    else:
        winners_count = min(giveaway["winners_count"], len(participants))
        winners = random.sample(participants, winners_count)

        winners_text = "\n".join(
            f"{i+1}. <a href='tg://user?id={w['user_id']}'>{w['full_name']}</a>"
            + (f" (@{w['username']})" if w.get("username") else "")
            for i, w in enumerate(winners)
        )

        text = (
            f"🎉 <b>Розыгрыш «{giveaway['title']}» завершён!</b>\n\n"
            f"Участников: {len(participants)}\n\n"
            f"🏆 <b>Победители:</b>\n{winners_text}\n\n"
            f"Поздравляем!"
        )

    try:
        await bot.send_message(
            chat_id=giveaway["channel_id"],
            text=text,
            parse_mode="HTML",
            reply_to_message_id=giveaway.get("message_id")
        )
        # Уведомляем создателя
        await bot.send_message(
            giveaway["creator_id"],
            f"✅ Розыгрыш #{giveaway_id} завершён!\n\n{text}",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка при завершении розыгрыша {giveaway_id}: {e}")


# ==================== ЗАПУСК ====================

async def main():
    await init_db()
    scheduler.start()
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
