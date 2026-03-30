import asyncio
import json
import logging
import os
import ssl
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import asyncpg
import certifi
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
    Update, FSInputFile, CallbackQuery,
)

from config import BOT_TOKEN, WEBAPP_URL


class PersistentAiohttpSession(AiohttpSession):
    """Keeps ClientSession alive across requests to avoid repeated TLS handshakes."""
    _client_session: aiohttp.ClientSession | None = None

    async def create_session(self) -> aiohttp.ClientSession:
        if self._client_session is None or self._client_session.closed:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_context, keepalive_timeout=300, limit=10)
            self._client_session = aiohttp.ClientSession(
                connector=connector,
                json_serialize=json.dumps,
            )
        return self._client_session

    async def close(self) -> None:
        if self._client_session and not self._client_session.closed:
            await self._client_session.close()
        await super().close()

logging.basicConfig(level=logging.INFO)

LOGS_DIR = "/logs"
ENABLE_LOGGING = os.environ.get("ENABLE_LOGGING", "true").lower() == "true"
_TZ = ZoneInfo(os.environ.get("TZ", "UTC"))

_pg_pool: asyncpg.Pool | None = None


async def get_pg_pool() -> asyncpg.Pool:
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = await asyncpg.create_pool(
            host=os.environ.get("PG_HOST", "postgres"),
            port=int(os.environ.get("PG_PORT", "5432")),
            database=os.environ.get("PG_DB", "habits"),
            user=os.environ.get("PG_USER", "habits"),
            password=os.environ.get("PG_PASSWORD", ""),
        )
    return _pg_pool


def _log_path(chat_id: int) -> str:
    os.makedirs(LOGS_DIR, exist_ok=True)
    prefix = "group" if chat_id < 0 else "direct"
    return os.path.join(LOGS_DIR, f"{prefix}_{abs(chat_id)}.log")


def log(chat_id: int, user_label: str, action: str):
    if not ENABLE_LOGGING:
        return
    ts = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    with open(_log_path(chat_id), "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {user_label}: {action}\n")


def format_user(user) -> str:
    name = user.first_name or ""
    if user.last_name:
        name += f" {user.last_name}"
    if user.username:
        return f"{name} (@{user.username}, id={user.id})"
    return f"{name} (id={user.id})"


class MessageLogMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Update, data: dict):
        if event.message and event.message.from_user:
            msg = event.message
            user_label = format_user(msg.from_user)
            text = msg.text or "[non-text]"
            if msg.chat.type in ("group", "supergroup"):
                action = f"message in \"{msg.chat.title}\" (group_id={msg.chat.id}): {text}"
            else:
                action = f"message: {text}"
            log(msg.chat.id, user_label, action)
        return await handler(event, data)


async def init_bot_db():
    pool = await get_pg_pool()
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            chat_id BIGINT PRIMARY KEY,
            title   TEXT NOT NULL
        )
    """)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS user_groups (
            user_id BIGINT NOT NULL,
            chat_id BIGINT NOT NULL,
            PRIMARY KEY (user_id, chat_id)
        )
    """)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)


async def get_config(key: str) -> str | None:
    pool = await get_pg_pool()
    row = await pool.fetchrow("SELECT value FROM config WHERE key=$1", key)
    return row["value"] if row else None


async def set_config(key: str, value: str):
    pool = await get_pg_pool()
    await pool.execute(
        "INSERT INTO config (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        key, value
    )


async def save_group(chat_id: int, title: str):
    pool = await get_pg_pool()
    await pool.execute(
        "INSERT INTO groups (chat_id, title) VALUES ($1, $2) ON CONFLICT (chat_id) DO UPDATE SET title=EXCLUDED.title",
        chat_id, title
    )


async def link_user_group(user_id: int, chat_id: int):
    pool = await get_pg_pool()
    await pool.execute(
        "INSERT INTO user_groups (user_id, chat_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        user_id, chat_id
    )


async def get_user_groups(user_id: int) -> list[dict]:
    pool = await get_pg_pool()
    rows = await pool.fetch(
        "SELECT g.chat_id, g.title FROM groups g JOIN user_groups ug ON g.chat_id=ug.chat_id WHERE ug.user_id=$1",
        user_id
    )
    return [dict(r) for r in rows]


async def get_group_title(chat_id: int) -> str | None:
    pool = await get_pg_pool()
    row = await pool.fetchrow("SELECT title FROM groups WHERE chat_id=$1", chat_id)
    return row["title"] if row else None


async def get_all_subscriptions() -> list[asyncpg.Record]:
    pool = await get_pg_pool()
    return await pool.fetch("""
        SELECT s.user_id, s.chat_id, g.title AS group_title, s.paid_until, s.trial_start
        FROM subscriptions s
        LEFT JOIN groups g ON g.chat_id = s.chat_id
        ORDER BY s.paid_until DESC NULLS LAST
    """)


async def sub_reset(user_id: int, chat_id: int):
    pool = await get_pg_pool()
    await pool.execute(
        "UPDATE subscriptions SET paid_until = NULL, trial_start = NOW() WHERE user_id=$1 AND chat_id=$2",
        user_id, chat_id
    )


async def sub_add_month(user_id: int, chat_id: int):
    pool = await get_pg_pool()
    await pool.execute("""
        UPDATE subscriptions
        SET paid_until = GREATEST(COALESCE(paid_until, NOW()), NOW()) + INTERVAL '30 days'
        WHERE user_id=$1 AND chat_id=$2
    """, user_id, chat_id)


def _sub_card(row: asyncpg.Record) -> tuple[str, InlineKeyboardMarkup]:
    uid, cid = row["user_id"], row["chat_id"]
    title = row["group_title"] or str(cid)
    paid_until = row["paid_until"]
    if paid_until:
        status = f"✅ until {paid_until.strftime('%Y-%m-%d')}" if paid_until > datetime.now(paid_until.tzinfo) else f"❌ expired {paid_until.strftime('%Y-%m-%d')}"
    else:
        status = "🕐 trial"
    text = f"<b>{title}</b>\nuser_id: <code>{uid}</code>\n{status}"
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Reset / Сброс", callback_data=f"sub_reset:{uid}:{cid}"),
        InlineKeyboardButton(text="➕ 1 month / +месяц", callback_data=f"sub_month:{uid}:{cid}"),
    ]])
    return text, markup


async def main():
    await init_bot_db()

    session = PersistentAiohttpSession(timeout=180)
    bot = Bot(token=BOT_TOKEN, session=session)
    dp = Dispatcher()
    dp.update.outer_middleware(MessageLogMiddleware())
    me = await bot.get_me()

    await bot.set_my_commands(
        [BotCommand(command="start", description="Открыть трекер привычек / Open habits tracker")],
        scope=BotCommandScopeAllGroupChats(),
    )
    await bot.set_my_commands(
        [BotCommand(command="start", description="Открыть трекер привычек / Open habits tracker")],
        scope=BotCommandScopeAllPrivateChats(),
    )

    @dp.message(CommandStart())
    async def cmd_start(message: Message, command: CommandObject):
        user_label = format_user(message.from_user)

        if message.chat.type in ("group", "supergroup"):
            is_first = await get_group_title(message.chat.id) is None
            await save_group(message.chat.id, message.chat.title or str(message.chat.id))
            miniapp_link = f"https://t.me/{me.username}?startapp=g{abs(message.chat.id)}"
            log(message.chat.id, user_label, f"used /start in group \"{message.chat.title}\"")
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Открыть трекер / Open tracker", url=miniapp_link)
            ]])
            if is_first:
                welcome_caption = (
                    "🏋️ <b>Трекер привычек / Habits tracker</b>\n\n"
                    "199 рублей в месяц!\n\n"
                    "Добавь в группу → /start → мини-приложение\n"
                    "Привычки, стрики, рейтинг\n\n"
                    "Add to a group → /start → mini app\n"
                    "Habits, streaks, leaderboard"
                )
                cached_id = await get_config("hello_photo_file_id")
                hello_path = "assets/hello.jpg"
                sent_photo = False
                photo = cached_id or (FSInputFile(hello_path) if os.path.exists(hello_path) else None)
                if photo:
                    try:
                        sent = await message.answer_photo(
                            photo=photo,
                            caption=welcome_caption,
                            reply_markup=markup,
                            parse_mode="HTML",
                        )
                        if not cached_id:
                            await set_config("hello_photo_file_id", sent.photo[-1].file_id)
                        sent_photo = True
                    except Exception:
                        logging.exception("send_photo failed")
                if not sent_photo:
                    await message.answer(
                        welcome_caption,
                        reply_markup=markup,
                        parse_mode="HTML",
                    )
            else:
                await message.answer(
                    f"Трекер привычек для <b>{message.chat.title}</b> / Habits tracker for <b>{message.chat.title}</b>:",
                    reply_markup=markup,
                    parse_mode="HTML",
                )
        else:
            args = command.args or ""
            if args.startswith("g"):
                group_id = int(args[1:])
                chat_id = -group_id
                await link_user_group(message.from_user.id, chat_id)
                title = await get_group_title(chat_id)
                if not title:
                    try:
                        chat = await bot.get_chat(chat_id)
                        title = chat.title or str(chat_id)
                        await save_group(chat_id, title)
                    except Exception:
                        title = str(chat_id)
                webapp_url = f"{WEBAPP_URL}?cid={chat_id}"
                log(message.chat.id, user_label, f"opened tracker for group \"{title}\" (cid={chat_id})")
                await message.answer(
                    f"Трекер для <b>{title}</b> / Tracker for <b>{title}</b>:",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text=f"Открыть · {title} / Open · {title}", web_app=WebAppInfo(url=webapp_url))
                    ]]),
                    parse_mode="HTML",
                )
            else:
                groups = await get_user_groups(message.from_user.id)
                log(message.chat.id, user_label, f"used /start in private chat")
                if not groups:
                    await message.answer(
                        "Добавь бота в группу и нажми /start там — здесь появятся кнопки для каждого чата.\n\n"
                        "Add the bot to a group and send /start there — buttons for each chat will appear here."
                    )
                else:
                    buttons = [
                        [InlineKeyboardButton(
                            text=g["title"],
                            web_app=WebAppInfo(url=f"{WEBAPP_URL}?cid={g['chat_id']}")
                        )]
                        for g in groups
                    ]
                    await message.answer(
                        "Выбери трекер / Choose tracker:",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                    )

    ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()]

    @dp.message(Command("admin"))
    async def cmd_admin(message: Message):
        if message.from_user.id not in ADMIN_IDS:
            return
        if message.chat.type != "private":
            return
        rows = await get_all_subscriptions()
        if not rows:
            await message.answer("No subscriptions yet.")
            return
        await message.answer(f"🔧 Admin panel — {len(rows)} subscription(s):")
        for row in rows:
            text, markup = _sub_card(row)
            await message.answer(text, reply_markup=markup, parse_mode="HTML")

    @dp.callback_query(lambda c: c.data and c.data.startswith("sub_reset:"))
    async def cb_sub_reset(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            await call.answer("Not authorized.", show_alert=True)
            return
        _, uid, cid = call.data.split(":")
        await sub_reset(int(uid), int(cid))
        pool = await get_pg_pool()
        rows = await pool.fetch(
            "SELECT s.user_id, s.chat_id, g.title AS group_title, s.paid_until, s.trial_start "
            "FROM subscriptions s LEFT JOIN groups g ON g.chat_id=s.chat_id "
            "WHERE s.user_id=$1 AND s.chat_id=$2", int(uid), int(cid)
        )
        if rows:
            text, markup = _sub_card(rows[0])
            await call.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await call.answer("✅ Reset to trial")

    @dp.callback_query(lambda c: c.data and c.data.startswith("sub_month:"))
    async def cb_sub_month(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            await call.answer("Not authorized.", show_alert=True)
            return
        _, uid, cid = call.data.split(":")
        await sub_add_month(int(uid), int(cid))
        pool = await get_pg_pool()
        rows = await pool.fetch(
            "SELECT s.user_id, s.chat_id, g.title AS group_title, s.paid_until, s.trial_start "
            "FROM subscriptions s LEFT JOIN groups g ON g.chat_id=s.chat_id "
            "WHERE s.user_id=$1 AND s.chat_id=$2", int(uid), int(cid)
        )
        if rows:
            text, markup = _sub_card(rows[0])
            await call.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await call.answer("✅ +30 days added")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
