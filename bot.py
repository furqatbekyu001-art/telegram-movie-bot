import asyncio
import json
import logging
import os
from pathlib import Path
from threading import Thread
from typing import Any

from flask import Flask
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


# Edit these values before starting the bot.
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
STORAGE_CHANNEL_ID = -1004328343198
ADMIN_IDS = {7921719616}

MOVIES_FILE = Path("movies.json")
STATS_FILE = Path("stats.json")
CHANNELS_FILE = Path("channels.json")
WEB_PORT = int(os.getenv("PORT", "5000"))
BROADCAST_DELAY_SECONDS = 0.1
BROADCAST_PENDING_KEY = "broadcast_pending"
BROADCAST_CONFIRM = "broadcast_confirm"
BROADCAST_CANCEL = "broadcast_cancel"
SUBSCRIPTION_CHECK = "subscription_check"
WAITING_FOR_BROADCAST_MESSAGE = 1
CONFIRMING_BROADCAST = 2
WAITING_FOR_ADD_CODE = 3
WAITING_FOR_ADD_CHANNEL = 4
WAITING_FOR_REMOVE_CHANNEL = 5

ADMIN_ADD_CODE_BUTTON = "🎬 Kod qo'shish"
ADMIN_LIST_CODES_BUTTON = "📁 Kodlar ro'yxati"
ADMIN_STATISTICS_BUTTON = "📊 Statistika"
ADMIN_BROADCAST_BUTTON = "📢 Xabar yuborish"
ADMIN_ADD_CHANNEL_BUTTON = "➕ Kanal qo'shish"
ADMIN_REMOVE_CHANNEL_BUTTON = "➖ Kanal o'chirish"
ADMIN_LIST_CHANNELS_BUTTON = "📋 Kanallar ro'yxati"

logger = logging.getLogger(__name__)

web_app = Flask(__name__)


@web_app.get("/")
def health_check() -> str:
    return "Bot is alive"


def run_web_server() -> None:
    web_app.run(
        host="0.0.0.0",
        port=WEB_PORT,
        use_reloader=False,
    )


def load_movies() -> dict[str, dict[str, int]]:
    """Load the code-to-message mapping, creating it when needed."""
    if not MOVIES_FILE.exists():
        return {}

    try:
        with MOVIES_FILE.open("r", encoding="utf-8") as file:
            data: Any = json.load(file)
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"Could not read {MOVIES_FILE}: {error}") from error

    if not isinstance(data, dict):
        raise RuntimeError(f"{MOVIES_FILE} must contain a JSON object")

    return data


def save_movies(movies: dict[str, dict[str, int]]) -> None:
    """Save the mapping in a readable JSON format."""
    with MOVIES_FILE.open("w", encoding="utf-8") as file:
        json.dump(movies, file, ensure_ascii=False, indent=2, sort_keys=True)


def normalize_channel(value: Any) -> str | int | None:
    """Normalize a channel username or numeric Telegram chat ID."""
    if isinstance(value, bool):
        return None

    text = str(value).strip()
    if not text:
        return None

    if text.startswith("-") and text[1:].isdigit():
        return int(text)
    if text.isdigit():
        return int(text)

    return f"@{text.lstrip('@')}"


def load_channels() -> list[str | int]:
    """Load the dynamic mandatory-subscription channel list."""
    if not CHANNELS_FILE.exists():
        return []

    try:
        with CHANNELS_FILE.open("r", encoding="utf-8") as file:
            data: Any = json.load(file)
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"Could not read {CHANNELS_FILE}: {error}") from error

    if not isinstance(data, list):
        raise RuntimeError(f"{CHANNELS_FILE} must contain a JSON list")

    channels: list[str | int] = []
    for item in data:
        channel = normalize_channel(item)
        if channel is None:
            raise RuntimeError(
                f"{CHANNELS_FILE} contains an invalid channel value: {item!r}"
            )
        if channel not in channels:
            channels.append(channel)
    return channels


def save_channels(channels: list[str | int]) -> None:
    """Save the mandatory-subscription channel list."""
    with CHANNELS_FILE.open("w", encoding="utf-8") as file:
        json.dump(channels, file, ensure_ascii=False, indent=2)


def load_stats() -> dict[str, Any]:
    """Load persistent user and movie-request statistics."""
    if not STATS_FILE.exists():
        return {"unique_users": [], "code_requests": {}}

    try:
        with STATS_FILE.open("r", encoding="utf-8") as file:
            data: Any = json.load(file)
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"Could not read {STATS_FILE}: {error}") from error

    if not isinstance(data, dict):
        raise RuntimeError(f"{STATS_FILE} must contain a JSON object")

    unique_users = data.get("unique_users", [])
    code_requests = data.get("code_requests", {})
    if not isinstance(unique_users, list) or not isinstance(code_requests, dict):
        raise RuntimeError(
            f"{STATS_FILE} must contain unique_users and code_requests"
        )

    normalized_users = []
    for user in unique_users:
        if isinstance(user, int):
            normalized_users.append({"id": user, "name": f"User {user}"})
        elif (
            isinstance(user, dict)
            and isinstance(user.get("id"), int)
            and isinstance(user.get("name"), str)
        ):
            normalized_users.append({"id": user["id"], "name": user["name"]})

    return {
        "unique_users": normalized_users,
        "code_requests": code_requests,
    }


def save_stats(stats: dict[str, Any]) -> None:
    """Save statistics in a readable JSON format."""
    with STATS_FILE.open("w", encoding="utf-8") as file:
        json.dump(stats, file, ensure_ascii=False, indent=2, sort_keys=True)


def record_user_interaction(stats: dict[str, Any], update: Update) -> None:
    user = update.effective_user
    if user is None:
        return

    display_name = (
        f"@{user.username}" if user.username else (user.first_name or f"User {user.id}")
    )
    for saved_user in stats["unique_users"]:
        if saved_user["id"] == user.id:
            saved_user["name"] = display_name
            return

    stats["unique_users"].append({"id": user.id, "name": display_name})


def format_statistics(stats: dict[str, Any]) -> str:
    code_requests = stats["code_requests"]
    total_requests = sum(
        count for count in code_requests.values() if isinstance(count, int)
    )
    top_codes = sorted(
        (
            (code, count)
            for code, count in code_requests.items()
            if isinstance(code, str) and isinstance(count, int)
        ),
        key=lambda item: (-item[1], item[0]),
    )[:5]

    lines = [
        "Statistika:",
        f"Noyob foydalanuvchilar: {len(stats['unique_users'])}",
        f"Jami kod so‘rovlari: {total_requests}",
        "Eng ko‘p so‘ralgan 5 ta kod:",
    ]
    if top_codes:
        lines.extend(
            f"{index}. {code} — {count}"
            for index, (code, count) in enumerate(top_codes, start=1)
        )
    else:
        lines.append("Hozircha so‘rovlar yo‘q.")

    lines.append("")
    lines.append("Foydalanuvchilar:")
    if stats["unique_users"]:
        lines.extend(
            f"- {user['name']} (ID: {user['id']})"
            for user in stats["unique_users"]
        )
    else:
        lines.append("Hozircha foydalanuvchilar yo‘q.")

    return "\n".join(lines)


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id in ADMIN_IDS


def admin_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [ADMIN_ADD_CODE_BUTTON, ADMIN_LIST_CODES_BUTTON],
            [ADMIN_STATISTICS_BUTTON, ADMIN_BROADCAST_BUTTON],
            [ADMIN_ADD_CHANNEL_BUTTON, ADMIN_REMOVE_CHANNEL_BUTTON],
            [ADMIN_LIST_CHANNELS_BUTTON],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


async def get_missing_required_channels(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> list[str | int]:
    missing: list[str | int] = []
    for channel in load_channels():
        try:
            member = await context.bot.get_chat_member(
                chat_id=channel,
                user_id=user_id,
            )
        except TelegramError as error:
            logger.warning(
                "Could not check subscription for %s and user %s: %s",
                channel,
                user_id,
                error,
            )
            missing.append(channel)
            continue

        if member.status == "left" or member.status == "kicked":
            missing.append(channel)
        elif member.status == "restricted" and not member.is_member:
            missing.append(channel)
    return missing


async def get_channel_join_link(
    context: ContextTypes.DEFAULT_TYPE,
    channel: str | int,
) -> str:
    if isinstance(channel, str) and channel.startswith("@"):
        return f"https://t.me/{channel[1:]}"

    try:
        chat = await context.bot.get_chat(chat_id=channel)
    except TelegramError:
        chat = None

    invite_link = getattr(chat, "invite_link", None)
    if isinstance(invite_link, str) and invite_link:
        return invite_link

    username = getattr(chat, "username", None)
    if isinstance(username, str) and username:
        return f"https://t.me/{username}"

    if isinstance(channel, int):
        channel_id = str(channel)
        if channel_id.startswith("-100"):
            channel_id = channel_id[4:]
        else:
            channel_id = channel_id.lstrip("-")
        return f"https://t.me/c/{channel_id}"

    return f"https://t.me/{str(channel).lstrip('@')}"


async def build_subscription_keyboard(
    context: ContextTypes.DEFAULT_TYPE,
    missing_channels: list[str | int],
) -> InlineKeyboardMarkup:
    rows = []
    for channel in missing_channels:
        join_link = await get_channel_join_link(context, channel)
        rows.append(
            [
                InlineKeyboardButton(
                    f"🔗 Join {channel}",
                    url=join_link,
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "✅ Tekshirish",
                callback_data=SUBSCRIPTION_CHECK,
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


async def ensure_subscription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    if is_admin(update):
        return True

    user = update.effective_user
    if user is None or not load_channels():
        return True

    missing_channels = await get_missing_required_channels(context, user.id)
    if not missing_channels:
        return True

    if update.message:
        await update.message.reply_text(
            "Botdan foydalanish uchun quyidagi kanallarga a’zo bo‘ling:",
            reply_markup=await build_subscription_keyboard(
                context,
                missing_channels,
            ),
        )
    return False


async def check_subscription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return

    if is_admin(update):
        await query.answer()
        return

    missing_channels = await get_missing_required_channels(context, user.id)
    if missing_channels:
        await query.answer(
            "Hali barcha kanallarga a’zo bo‘lmagansiz.",
            show_alert=True,
        )
        try:
            await query.edit_message_reply_markup(
                reply_markup=await build_subscription_keyboard(
                    context,
                    missing_channels,
                )
            )
        except TelegramError:
            pass
        return

    await query.answer("Obuna tasdiqlandi!")
    try:
        await query.edit_message_text(
            "✅ Obuna tasdiqlandi. Endi kino kodini yuboring."
        )
    except TelegramError:
        pass


def is_forwarded_message(message: Any) -> bool:
    """Return whether a message was forwarded from another chat or user."""
    if getattr(message, "forward_origin", None) is not None:
        return True

    return any(
        getattr(message, attribute, None) is not None
        for attribute in (
            "forward_from",
            "forward_from_chat",
            "forward_sender_name",
            "forward_signature",
        )
    )


def get_forwarded_storage_message(
    message: Any,
) -> tuple[int | None, int | None]:
    """
    Return (source_chat_id, original_message_id) for a forwarded message.

    Newer python-telegram-bot versions expose forward_origin. The fallback
    supports the older forward_from_chat/forward_from_message_id fields too.
    """
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        origin_chat = getattr(origin, "chat", None)
        origin_chat_id = getattr(origin_chat, "id", None)
        origin_message_id = getattr(origin, "message_id", None)
        return origin_chat_id, origin_message_id

    source_chat = getattr(message, "forward_from_chat", None)
    source_chat_id = getattr(source_chat, "id", None)
    source_message_id = getattr(message, "forward_from_message_id", None)
    return source_chat_id, source_message_id


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        stats = load_stats()
        record_user_interaction(stats, update)
        save_stats(stats)
        if is_admin(update):
            await update.message.reply_text(
                "Assalomu alaykum! Kino kodini yuboring (masalan: KINO001).",
                reply_markup=admin_reply_keyboard(),
            )
            return

        if not await ensure_subscription(update, context):
            return

        await update.message.reply_text(
            "Assalomu alaykum! Kino kodini yuboring (masalan: KINO001)."
        )


async def find_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    stats = load_stats()
    record_user_interaction(stats, update)
    if not await ensure_subscription(update, context):
        save_stats(stats)
        return

    code = update.message.text.strip().upper()
    if code != "STATISTIK":
        stats["code_requests"][code] = stats["code_requests"].get(code, 0) + 1
    save_stats(stats)
    movies = load_movies()
    movie = movies.get(code)

    if not movie:
        await update.message.reply_text("Kod topilmadi")
        return

    message_id = movie.get("message_id")
    if not isinstance(message_id, int):
        await update.message.reply_text("Kod topilmadi")
        return

    try:
        await context.bot.copy_message(
            chat_id=update.effective_chat.id,
            from_chat_id=STORAGE_CHANNEL_ID,
            message_id=message_id,
        )
    except Exception:
        await update.message.reply_text(
            "Kino yuborilmadi. Keyinroq qayta urinib ko‘ring."
        )


async def save_code_from_reply(update: Update, code: str) -> bool:
    if not update.message:
        return False

    replied_message = update.message.reply_to_message
    if replied_message is None:
        await update.message.reply_text(
            "Bu buyruqni storage kanalidan forward qilingan xabarga reply qilib yuboring."
        )
        return False

    source_chat_id, original_message_id = get_forwarded_storage_message(
        replied_message
    )
    if (
        source_chat_id != STORAGE_CHANNEL_ID
        or not isinstance(original_message_id, int)
    ):
        await update.message.reply_text(
            "Faqat storage kanalidan forward qilingan xabarga reply qiling."
        )
        return False

    movies = load_movies()
    movies[code] = {"message_id": original_message_id}
    save_movies(movies)
    await update.message.reply_text(f"{code} kodi saqlandi.")
    return True


async def add_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    stats = load_stats()
    record_user_interaction(stats, update)
    save_stats(stats)

    if not is_admin(update):
        await update.message.reply_text("Sizda bu buyruqni ishlatish huquqi yo‘q.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Foydalanish: /addcode CODE")
        return

    await save_code_from_reply(update, context.args[0].strip().upper())


async def list_codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message:
        return

    stats = load_stats()
    record_user_interaction(stats, update)
    save_stats(stats)

    if not is_admin(update):
        await update.message.reply_text("Sizda bu buyruqni ishlatish huquqi yo‘q.")
        return

    movies = load_movies()
    if not movies:
        await update.message.reply_text("Hozircha hech qanday kod saqlanmagan.")
        return

    codes = "\n".join(sorted(movies))
    await update.message.reply_text(f"Saqlangan kodlar:\n{codes}")


async def statistics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message:
        return

    stats = load_stats()
    record_user_interaction(stats, update)
    save_stats(stats)

    if not is_admin(update):
        await update.message.reply_text("Sizda bu buyruqni ishlatish huquqi yo‘q.")
        return

    await update.message.reply_text(format_statistics(stats))


async def change_required_channel(
    update: Update,
    channel_value: str,
    *,
    adding: bool,
) -> None:
    if not update.message:
        return

    channel = normalize_channel(channel_value)
    if channel is None:
        await update.message.reply_text(
            "Kanal username yoki ID sini yuboring (masalan: @kanal)."
        )
        return

    channels = load_channels()
    if adding:
        if channel in channels:
            await update.message.reply_text(f"{channel} allaqachon ro‘yxatda.")
            return
        channels.append(channel)
        save_channels(channels)
        await update.message.reply_text(f"{channel} majburiy kanal sifatida qo‘shildi.")
        return

    if channel not in channels:
        await update.message.reply_text(f"{channel} ro‘yxatda topilmadi.")
        return
    channels.remove(channel)
    save_channels(channels)
    await update.message.reply_text(f"{channel} majburiy kanallar ro‘yxatidan o‘chirildi.")


async def add_required_channel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not update.message:
        return ConversationHandler.END

    stats = load_stats()
    record_user_interaction(stats, update)
    save_stats(stats)

    if not is_admin(update):
        await update.message.reply_text("Sizda bu buyruqni ishlatish huquqi yo‘q.")
        return ConversationHandler.END

    args = context.args or []
    if len(args) > 1:
        await update.message.reply_text("Foydalanish: /kanalqoshish @channelusername")
        return ConversationHandler.END
    if len(args) == 1:
        await change_required_channel(update, args[0], adding=True)
        return ConversationHandler.END

    await update.message.reply_text(
        "Majburiy kanal username yoki ID sini yuboring (masalan: @kanal):"
    )
    return WAITING_FOR_ADD_CHANNEL


async def remove_required_channel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not update.message:
        return ConversationHandler.END

    stats = load_stats()
    record_user_interaction(stats, update)
    save_stats(stats)

    if not is_admin(update):
        await update.message.reply_text("Sizda bu buyruqni ishlatish huquqi yo‘q.")
        return ConversationHandler.END

    args = context.args or []
    if len(args) > 1:
        await update.message.reply_text("Foydalanish: /kanalochir @channelusername")
        return ConversationHandler.END
    if len(args) == 1:
        await change_required_channel(update, args[0], adding=False)
        return ConversationHandler.END

    await update.message.reply_text(
        "O‘chiriladigan kanal username yoki ID sini yuboring (masalan: @kanal):"
    )
    return WAITING_FOR_REMOVE_CHANNEL


async def list_required_channels(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    del context
    if not update.message:
        return

    stats = load_stats()
    record_user_interaction(stats, update)
    save_stats(stats)

    if not is_admin(update):
        await update.message.reply_text("Sizda bu buyruqni ishlatish huquqi yo‘q.")
        return

    channels = load_channels()
    if not channels:
        await update.message.reply_text("Hozircha majburiy kanallar ro‘yxati bo‘sh.")
        return

    channel_lines = "\n".join(f"{index}. {channel}" for index, channel in enumerate(channels, 1))
    await update.message.reply_text(f"Majburiy kanallar:\n{channel_lines}")


async def start_add_code_from_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    del context
    if not update.message:
        return ConversationHandler.END

    if not is_admin(update):
        await update.message.reply_text("Sizda bu buyruqni ishlatish huquqi yo‘q.")
        return ConversationHandler.END

    stats = load_stats()
    record_user_interaction(stats, update)
    save_stats(stats)
    await update.message.reply_text(
        "Kino kodini storage kanalidan forward qilingan xabarga reply qilib yuboring "
        "(masalan: KINO001):"
    )
    return WAITING_FOR_ADD_CODE


async def receive_add_code_from_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    del context
    if not update.message or not is_admin(update):
        return ConversationHandler.END
    if not update.message.text:
        await update.message.reply_text(
            "Kino kodini storage kanalidan forward qilingan xabarga reply qilib yuboring."
        )
        return WAITING_FOR_ADD_CODE

    text = update.message.text.strip()
    parts = text.split()
    if parts and parts[0].lower().split("@", 1)[0] == "/addcode":
        if len(parts) != 2:
            await update.message.reply_text("Kodni yuboring (masalan: KINO001).")
            return WAITING_FOR_ADD_CODE
        code = parts[1].strip().upper()
    else:
        code = text.upper()

    if not code:
        await update.message.reply_text("Kodni yuboring (masalan: KINO001).")
        return WAITING_FOR_ADD_CODE

    saved = await save_code_from_reply(update, code)
    return ConversationHandler.END if saved else WAITING_FOR_ADD_CODE


async def receive_add_channel_from_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    del context
    if not update.message or not is_admin(update):
        return ConversationHandler.END
    if not update.message.text:
        await update.message.reply_text(
            "Kanal username yoki ID sini yuboring (masalan: @kanal)."
        )
        return WAITING_FOR_ADD_CHANNEL

    await change_required_channel(update, update.message.text, adding=True)
    return ConversationHandler.END


async def receive_remove_channel_from_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    del context
    if not update.message or not is_admin(update):
        return ConversationHandler.END
    if not update.message.text:
        await update.message.reply_text(
            "Kanal username yoki ID sini yuboring (masalan: @kanal)."
        )
        return WAITING_FOR_REMOVE_CHANNEL

    await change_required_channel(update, update.message.text, adding=False)
    return ConversationHandler.END


async def admin_menu_list_codes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    await list_codes(update, context)
    return ConversationHandler.END


async def admin_menu_statistics(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    await statistics(update, context)
    return ConversationHandler.END


async def admin_menu_list_channels(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    await list_required_channels(update, context)
    return ConversationHandler.END


def broadcast_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Tasdiqlash",
                    callback_data=BROADCAST_CONFIRM,
                ),
                InlineKeyboardButton(
                    "❌ Bekor qilish",
                    callback_data=BROADCAST_CANCEL,
                ),
            ]
        ]
    )


async def show_broadcast_preview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    source_chat_id: int,
    source_message_id: int,
) -> bool:
    """Copy the pending message back to the admin with confirmation buttons."""
    context.user_data[BROADCAST_PENDING_KEY] = (
        source_chat_id,
        source_message_id,
    )

    try:
        await context.bot.copy_message(
            chat_id=source_chat_id,
            from_chat_id=source_chat_id,
            message_id=source_message_id,
            reply_markup=broadcast_keyboard(),
        )
    except Exception as error:
        context.user_data.pop(BROADCAST_PENDING_KEY, None)
        logger.warning("Could not create broadcast preview: %s", error)
        if update.message:
            await update.message.reply_text(
                "Bu xabarni preview qilib bo‘lmadi. Boshqa xabar yuboring."
            )
        return False

    return True


async def start_broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not update.message:
        return ConversationHandler.END

    stats = load_stats()
    record_user_interaction(stats, update)
    save_stats(stats)

    if not is_admin(update):
        await update.message.reply_text("Sizda bu buyruqni ishlatish huquqi yo‘q.")
        return ConversationHandler.END

    replied_message = update.message.reply_to_message
    source_chat_id = update.message.chat_id
    if (
        replied_message is not None
        and is_forwarded_message(replied_message)
        and isinstance(source_chat_id, int)
    ):
        preview_created = await show_broadcast_preview(
            update,
            context,
            source_chat_id,
            replied_message.message_id,
        )
        return CONFIRMING_BROADCAST if preview_created else ConversationHandler.END

    await update.message.reply_text(
        "Xabaringizni yuboring (matn, rasm, yoki ikkisi birga):"
    )
    return WAITING_FOR_BROADCAST_MESSAGE


async def receive_broadcast_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not update.message or not is_admin(update):
        return ConversationHandler.END

    source_chat_id = update.message.chat_id
    if not isinstance(source_chat_id, int):
        return ConversationHandler.END

    preview_created = await show_broadcast_preview(
        update,
        context,
        source_chat_id,
        update.message.message_id,
    )
    return CONFIRMING_BROADCAST if preview_created else ConversationHandler.END


async def confirm_broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    if query is None:
        return ConversationHandler.END

    if not is_admin(update):
        await query.answer("Sizda bu amalni bajarish huquqi yo‘q.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    pending = context.user_data.pop(BROADCAST_PENDING_KEY, None)
    if (
        not isinstance(pending, tuple)
        or len(pending) != 2
        or not all(isinstance(value, int) for value in pending)
    ):
        await query.edit_message_reply_markup(reply_markup=None)
        if query.message:
            await query.message.reply_text("Yuboriladigan xabar topilmadi.")
        return ConversationHandler.END

    source_chat_id, source_message_id = pending
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    stats = load_stats()
    stored_users = stats.get("unique_users", [])
    user_ids = list(
        dict.fromkeys(
            user["id"]
            for user in stored_users
            if isinstance(user, dict) and isinstance(user.get("id"), int)
        )
    )

    succeeded = 0
    failed = 0
    for user_id in user_ids:
        try:
            await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
            )
            succeeded += 1
        except Exception as error:
            failed += 1
            logger.warning("Broadcast failed for user %s: %s", user_id, error)
        await asyncio.sleep(BROADCAST_DELAY_SECONDS)

    if query.message:
        await query.message.reply_text(
            "Xabar yuborish yakunlandi.\n"
            f"Muvaffaqiyatli: {succeeded}\n"
            f"Muvaffaqiyatsiz: {failed}"
        )
    return ConversationHandler.END


async def cancel_broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    if query is None:
        return ConversationHandler.END

    if not is_admin(update):
        await query.answer("Sizda bu amalni bajarish huquqi yo‘q.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    context.user_data.pop(BROADCAST_PENDING_KEY, None)
    if query.message:
        try:
            await query.message.delete()
        except TelegramError:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("Xabar bekor qilindi.")
    return ConversationHandler.END


async def cancel_broadcast_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    had_broadcast = context.user_data.pop(BROADCAST_PENDING_KEY, None) is not None
    if update.message:
        await update.message.reply_text(
            "Xabar bekor qilindi." if had_broadcast else "Amal bekor qilindi."
        )
    return ConversationHandler.END


async def configure_command_menu(application: Application) -> None:
    regular_commands = [
        BotCommand("start", "Botni boshlash"),
    ]
    admin_commands = [
        BotCommand("start", "Botni boshlash"),
        BotCommand("addcode", "Kino kodini qo‘shish"),
        BotCommand("listcodes", "Barcha kodlarni ko‘rish"),
        BotCommand("statistik", "Bot statistikasini ko‘rish"),
        BotCommand("xabar", "Foydalanuvchilarga xabar yuborish"),
        BotCommand("kanalqoshish", "Majburiy kanal qo‘shish"),
        BotCommand("kanalochir", "Majburiy kanalni o‘chirish"),
        BotCommand("kanallar", "Majburiy kanallar ro‘yxati"),
    ]

    await application.bot.set_my_commands(
        regular_commands,
        scope=BotCommandScopeDefault(),
    )
    for admin_id in ADMIN_IDS:
        await application.bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=admin_id),
        )


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("Set the BOT_TOKEN workspace secret before starting the bot.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(configure_command_menu)
        .build()
    )
    application.add_handler(
        CallbackQueryHandler(
            check_subscription,
            pattern=f"^{SUBSCRIPTION_CHECK}$",
        )
    )
    broadcast_handler = ConversationHandler(
        entry_points=[
            CommandHandler("xabar", start_broadcast),
            MessageHandler(
                filters.Regex(f"^{ADMIN_BROADCAST_BUTTON}$"),
                start_broadcast,
            ),
            CommandHandler("kanalqoshish", add_required_channel),
            MessageHandler(
                filters.Regex(f"^{ADMIN_ADD_CHANNEL_BUTTON}$"),
                add_required_channel,
            ),
            CommandHandler("kanalochir", remove_required_channel),
            MessageHandler(
                filters.Regex(f"^{ADMIN_REMOVE_CHANNEL_BUTTON}$"),
                remove_required_channel,
            ),
            MessageHandler(
                filters.Regex(f"^{ADMIN_ADD_CODE_BUTTON}$"),
                start_add_code_from_menu,
            ),
            MessageHandler(
                filters.Regex(f"^{ADMIN_LIST_CODES_BUTTON}$"),
                admin_menu_list_codes,
            ),
            MessageHandler(
                filters.Regex(f"^{ADMIN_STATISTICS_BUTTON}$"),
                admin_menu_statistics,
            ),
            MessageHandler(
                filters.Regex(f"^{ADMIN_LIST_CHANNELS_BUTTON}$"),
                admin_menu_list_channels,
            ),
        ],
        states={
            WAITING_FOR_BROADCAST_MESSAGE: [
                MessageHandler(filters.ALL, receive_broadcast_message),
            ],
            CONFIRMING_BROADCAST: [
                CallbackQueryHandler(
                    confirm_broadcast,
                    pattern=f"^{BROADCAST_CONFIRM}$",
                ),
                CallbackQueryHandler(
                    cancel_broadcast,
                    pattern=f"^{BROADCAST_CANCEL}$",
                ),
            ],
            WAITING_FOR_ADD_CODE: [
                MessageHandler(filters.ALL, receive_add_code_from_menu),
            ],
            WAITING_FOR_ADD_CHANNEL: [
                MessageHandler(filters.ALL, receive_add_channel_from_menu),
            ],
            WAITING_FOR_REMOVE_CHANNEL: [
                MessageHandler(filters.ALL, receive_remove_channel_from_menu),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_broadcast_command)],
    )
    application.add_handler(broadcast_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addcode", add_code))
    application.add_handler(CommandHandler("listcodes", list_codes))
    application.add_handler(CommandHandler("statistik", statistics))
    application.add_handler(CommandHandler("kanallar", list_required_channels))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, find_movie)
    )

    Thread(
        target=run_web_server,
        name="bot-health-server",
        daemon=True,
    ).start()
    application.run_polling()


if __name__ == "__main__":
    main()