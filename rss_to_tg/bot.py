from __future__ import annotations

import logging
from html import escape
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .config import Settings
from .database import Database
from .poller import FeedPoller
from .repository import DuplicateFeedError, FeedRepository
from .scheduler import FeedScheduler

logger = logging.getLogger(__name__)

ADD_URL, ADD_CHANNEL, ADD_INTERVAL = range(3)
EDIT_FIELD, EDIT_VALUE = range(3, 5)


def _authorized(update: Update, settings: Settings) -> bool:
    return update.effective_user is not None and update.effective_user.id in settings.admin_user_ids


async def _deny(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text("You are not authorized to manage this bot.")


async def _guard(update: Update, settings: Settings) -> bool:
    if _authorized(update, settings):
        return True
    await _deny(update)
    return False


def _valid_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _interval(value: str, settings: Settings) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError("The interval must be a whole number of minutes.") from exc
    if not settings.min_interval_minutes <= result <= settings.max_interval_minutes:
        raise ValueError(
            f"The interval must be between {settings.min_interval_minutes} and "
            f"{settings.max_interval_minutes} minutes."
        )
    return result


async def _resolve_channel(context: CallbackContext, value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("A channel username or numeric channel ID is required.")
    if not (value.startswith("@") or value.lstrip("-").isdigit()):
        raise ValueError("Use a public channel username such as @my_channel or a numeric channel ID.")
    chat = await context.bot.get_chat(value)
    if chat.type != ChatType.CHANNEL:
        raise ValueError("That chat is not a Telegram channel.")
    return str(chat.id)


async def _create_feed(
    update: Update,
    context: CallbackContext,
    settings: Settings,
    repository: FeedRepository,
    url: str,
    channel: str,
    interval: int,
) -> None:
    if not _valid_url(url):
        await update.effective_message.reply_text("Feed URL must start with http:// or https://.")
        return
    try:
        minutes = _interval(str(interval), settings)
        channel_id = await _resolve_channel(context, channel)
        feed = await repository.create_feed(
            url=url.strip(),
            channel_id=channel_id,
            interval_minutes=minutes,
            created_by=update.effective_user.id,
        )
    except DuplicateFeedError:
        await update.effective_message.reply_text("That feed is already connected to this channel.")
        return
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    except Exception:
        logger.exception("Could not add feed")
        await update.effective_message.reply_text(
            "I could not access that channel. Make sure I am an administrator in it and try again."
        )
        return
    await update.effective_message.reply_text(
        f"Feed <b>#{feed.id}</b> added for channel <code>{escape(feed.channel_id)}</code>.\n"
        f"I will check it every {feed.interval_minutes} minutes.",
        parse_mode="HTML",
    )


async def add_start(update: Update, context: CallbackContext) -> int:
    settings: Settings = context.application.bot_data["settings"]
    if not await _guard(update, settings):
        return ConversationHandler.END
    if len(context.args) >= 2:
        interval = context.args[2] if len(context.args) >= 3 else str(settings.default_interval_minutes)
        try:
            minutes = _interval(interval, settings)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return ConversationHandler.END
        await _create_feed(
            update,
            context,
            settings,
            context.application.bot_data["repository"],
            context.args[0],
            context.args[1],
            minutes,
        )
        return ConversationHandler.END
    context.user_data["new_feed"] = {}
    await update.effective_message.reply_text(
        "Send the RSS/Atom feed URL, or /cancel to stop."
    )
    return ADD_URL


async def add_url(update: Update, context: CallbackContext) -> int:
    value = update.effective_message.text.strip()
    if not _valid_url(value):
        await update.effective_message.reply_text("Please send a valid http(s) feed URL.")
        return ADD_URL
    context.user_data["new_feed"]["url"] = value
    await update.effective_message.reply_text(
        "Now send the channel username (for example @news) or its numeric channel ID.\n"
        "The bot must be an administrator in that channel."
    )
    return ADD_CHANNEL


async def add_channel(update: Update, context: CallbackContext) -> int:
    settings: Settings = context.application.bot_data["settings"]
    try:
        context.user_data["new_feed"]["channel"] = await _resolve_channel(
            context, update.effective_message.text
        )
    except Exception as exc:
        await update.effective_message.reply_text(str(exc))
        return ADD_CHANNEL
    await update.effective_message.reply_text(
        f"How often should I check it? Send minutes ({settings.default_interval_minutes} by default)."
    )
    return ADD_INTERVAL


async def add_interval(update: Update, context: CallbackContext) -> int:
    settings: Settings = context.application.bot_data["settings"]
    try:
        minutes = _interval(update.effective_message.text, settings)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return ADD_INTERVAL
    values = context.user_data.pop("new_feed", {})
    await _create_feed(
        update,
        context,
        settings,
        context.application.bot_data["repository"],
        values["url"],
        values["channel"],
        minutes,
    )
    return ConversationHandler.END


async def edit_start(update: Update, context: CallbackContext) -> int:
    settings: Settings = context.application.bot_data["settings"]
    if not await _guard(update, settings):
        return ConversationHandler.END
    if not context.args:
        await update.effective_message.reply_text("Usage: /editfeed <id>")
        return ConversationHandler.END
    try:
        feed_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Feed ID must be a number.")
        return ConversationHandler.END
    feed = await context.application.bot_data["repository"].get_feed(feed_id)
    if feed is None:
        await update.effective_message.reply_text("Feed not found.")
        return ConversationHandler.END
    context.user_data["edit_feed_id"] = feed_id
    await update.effective_message.reply_text(
        "Which field should change? Reply with one of: url, channel, interval, enabled."
    )
    return EDIT_FIELD


async def edit_field(update: Update, context: CallbackContext) -> int:
    field = update.effective_message.text.strip().lower()
    if field not in {"url", "channel", "interval", "enabled"}:
        await update.effective_message.reply_text("Choose url, channel, interval, or enabled.")
        return EDIT_FIELD
    context.user_data["edit_field"] = field
    await update.effective_message.reply_text(f"Send the new value for {field}.")
    return EDIT_VALUE


async def edit_value(update: Update, context: CallbackContext) -> int:
    settings: Settings = context.application.bot_data["settings"]
    repository: FeedRepository = context.application.bot_data["repository"]
    field = context.user_data.pop("edit_field")
    feed_id = context.user_data.pop("edit_feed_id")
    value = update.effective_message.text.strip()
    try:
        if field == "url":
            if not _valid_url(value):
                raise ValueError("Feed URL must start with http:// or https://.")
            values = {"url": value}
        elif field == "channel":
            values = {"channel_id": await _resolve_channel(context, value)}
        elif field == "interval":
            values = {"interval_minutes": _interval(value, settings)}
        else:
            if value.lower() not in {"on", "off", "true", "false", "yes", "no"}:
                raise ValueError("Enabled must be on or off.")
            values = {"enabled": value.lower() in {"on", "true", "yes"}}
        await repository.update_feed(feed_id, **values)
    except DuplicateFeedError:
        await update.effective_message.reply_text("That feed is already connected to this channel.")
        return ConversationHandler.END
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return ConversationHandler.END
    except Exception:
        logger.exception("Could not edit feed %s", feed_id)
        await update.effective_message.reply_text("I could not update that feed. Check the channel access and try again.")
        return ConversationHandler.END
    await update.effective_message.reply_text(f"Feed #{feed_id} updated.")
    return ConversationHandler.END


async def cancel(update: Update, context: CallbackContext) -> int:
    context.user_data.pop("new_feed", None)
    context.user_data.pop("edit_feed_id", None)
    context.user_data.pop("edit_field", None)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


async def list_feeds(update: Update, context: CallbackContext) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await _guard(update, settings):
        return
    feeds = await context.application.bot_data["repository"].list_feeds()
    if not feeds:
        await update.effective_message.reply_text("No feeds configured yet. Use /addfeed to add one.")
        return
    lines = ["<b>Configured feeds</b>"]
    for feed in feeds:
        status = "enabled" if feed.enabled else "paused"
        error = f"\n   ⚠️ {escape(feed.last_error[:180])}" if feed.last_error else ""
        lines.append(
            f"\n<b>#{feed.id}</b> · {status} · every {feed.interval_minutes} min\n"
            f"<code>{escape(feed.channel_id)}</code> · <a href=\"{escape(feed.url, quote=True)}\">feed</a>"
            f"{error}"
        )
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def delete_start(update: Update, context: CallbackContext) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await _guard(update, settings):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /deletefeed <id>")
        return
    feed_id = int(context.args[0])
    feed = await context.application.bot_data["repository"].get_feed(feed_id)
    if feed is None:
        await update.effective_message.reply_text("Feed not found.")
        return
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Delete", callback_data=f"delete:confirm:{feed_id}"),
            InlineKeyboardButton("Cancel", callback_data=f"delete:cancel:{feed_id}"),
        ]]
    )
    await update.effective_message.reply_text(
        f"Delete feed #{feed_id} and its article history?", reply_markup=keyboard
    )


async def delete_callback(update: Update, context: CallbackContext) -> None:
    settings: Settings = context.application.bot_data["settings"]
    query = update.callback_query
    if not _authorized(update, settings):
        await query.answer("Not authorized", show_alert=True)
        return
    await query.answer()
    action, decision, raw_id = query.data.split(":")
    if action != "delete":
        return
    if not raw_id.isdigit():
        await query.edit_message_text("Invalid feed ID.")
        return
    feed_id = int(raw_id)
    if decision == "confirm":
        deleted = await context.application.bot_data["repository"].delete_feed(feed_id)
        await query.edit_message_text("Feed deleted." if deleted else "Feed was already deleted.")
    else:
        await query.edit_message_text("Deletion cancelled.")


async def toggle_feed(update: Update, context: CallbackContext) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await _guard(update, settings):
        return
    if len(context.args) != 2 or not context.args[0].isdigit() or context.args[1].lower() not in {"on", "off"}:
        await update.effective_message.reply_text("Usage: /togglefeed <id> on|off")
        return
    feed_id = int(context.args[0])
    feed = await context.application.bot_data["repository"].update_feed(
        feed_id, enabled=context.args[1].lower() == "on"
    )
    await update.effective_message.reply_text(
        f"Feed #{feed_id} is now {'enabled' if feed and feed.enabled else 'paused'}."
        if feed
        else "Feed not found."
    )


async def stats(update: Update, context: CallbackContext) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await _guard(update, settings):
        return
    repository: FeedRepository = context.application.bot_data["repository"]
    feeds = await repository.list_feeds()
    total, posted = await repository.article_stats()
    await update.effective_message.reply_text(
        f"Feeds: {len(feeds)} ({sum(feed.enabled for feed in feeds)} enabled)\n"
        f"Articles tracked: {total}\nArticles posted: {posted}"
    )


async def help_command(update: Update, context: CallbackContext) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await _guard(update, settings):
        return
    await update.effective_message.reply_text(
        "<b>RSS-to-Telegram</b>\n\n"
        "/addfeed — add interactively\n"
        "/addfeed URL CHANNEL [MINUTES] — add directly\n"
        "/feeds — list configured feeds\n"
        "/editfeed ID — change one field\n"
        "/togglefeed ID on|off — pause or resume\n"
        "/deletefeed ID — delete with confirmation\n"
        "/stats — show counters\n"
        "/cancel — cancel an active prompt",
        parse_mode="HTML",
    )


async def error_handler(update: object, context: CallbackContext) -> None:
    logger.error("Unhandled Telegram update error", exc_info=context.error)


def build_application(
    settings: Settings,
    database: Database,
    poller: FeedPoller,
    scheduler: FeedScheduler,
) -> Application:
    repository = FeedRepository(database)

    async def post_shutdown(_application: Application) -> None:
        await poller.close()

    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(scheduler.start)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data.update(
        settings=settings,
        repository=repository,
        poller=poller,
    )

    add_conversation = ConversationHandler(
        entry_points=[CommandHandler("addfeed", add_start)],
        states={
            ADD_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_url)],
            ADD_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_channel)],
            ADD_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_interval)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    edit_conversation = ConversationHandler(
        entry_points=[CommandHandler("editfeed", edit_start)],
        states={
            EDIT_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_field)],
            EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    application.add_handler(CommandHandler(["start", "help"], help_command))
    application.add_handler(add_conversation)
    application.add_handler(edit_conversation)
    application.add_handler(CommandHandler("feeds", list_feeds))
    application.add_handler(CommandHandler("deletefeed", delete_start))
    application.add_handler(CommandHandler("togglefeed", toggle_feed))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(delete_callback, pattern=r"^delete:"))
    application.add_error_handler(error_handler)
    return application
