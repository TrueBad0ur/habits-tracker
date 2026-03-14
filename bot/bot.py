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
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
    Update,
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


async def main():
    await init_bot_db()

    session = PersistentAiohttpSession(timeout=180)
    bot = Bot(token=BOT_TOKEN, session=session)
    dp = Dispatcher()
    dp.update.outer_middleware(MessageLogMiddleware())
    me = await bot.get_me()

    await bot.set_my_commands(
        [BotCommand(command="start", description="Открыть трекер привычек")],
        scope=BotCommandScopeAllGroupChats(),
    )
    await bot.set_my_commands(
        [BotCommand(command="start", description="Открыть трекер привычек")],
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
                InlineKeyboardButton(text="Открыть трекер", url=miniapp_link)
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
                await message.answer(
                    welcome_caption,
                    reply_markup=markup,
                    parse_mode="HTML",
                )
            else:
                await message.answer(
                    f"Трекер привычек для <b>{message.chat.title}</b>:",
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
                    f"Трекер для <b>{title}</b>:",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text=f"Открыть · {title}", web_app=WebAppInfo(url=webapp_url))
                    ]]),
                    parse_mode="HTML",
                )
            else:
                groups = await get_user_groups(message.from_user.id)
                log(message.chat.id, user_label, f"used /start in private chat")
                if not groups:
                    await message.answer(
                        "Добавь бота в группу и нажми /start там — "
                        "здесь появятся кнопки для каждого чата."
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
                        "Выбери трекер:",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
