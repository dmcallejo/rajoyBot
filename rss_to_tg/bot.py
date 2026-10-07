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
from .repository import DuplicateConfigurationError, FeedRepository
from .scheduler import FeedScheduler

logger = logging.getLogger(__name__)

ADD_NAME, ADD_URL, ADD_CHANNEL, ADD_INTERVAL = range(4)
EDIT_FIELD, EDIT_VALUE = range(4, 6)


def _has_private_user(update: Update) -> bool:
    return (
        update.effective_user is not None
        and update.effective_chat is not None
        and update.effective_chat.type == ChatType.PRIVATE
    )


async def _guard(update: Update, _settings: Settings) -> bool:
    if _has_private_user(update):
        return True
    if update.effective_message:
        await update.effective_message.reply_text(
            "Manage your feed configurations in a private chat with me."
        )
    return False


def _valid_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _configuration_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("A configuration name is required.")
    if len(name) > 100:
        raise ValueError("Configuration names can be at most 100 characters.")
    if any(ord(character) < 32 for character in name):
        raise ValueError("The configuration name contains an invalid character.")
    return name


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


async def _resolve_channel(context: CallbackContext, value: str, user_id: int) -> str:
    value = value.strip()
    if not value:
        raise ValueError("A channel username or numeric channel ID is required.")
    if not (value.startswith("@") or value.lstrip("-").isdigit()):
        raise ValueError("Use a public channel username such as @my_channel or a numeric channel ID.")
    chat = await context.bot.get_chat(value)
    if chat.type != ChatType.CHANNEL:
        raise ValueError("That chat is not a Telegram channel.")
    bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
    admin_statuses = {"administrator", "creator"}
    if bot_member.status not in admin_statuses or (
        bot_member.status == "administrator" and not bot_member.can_post_messages
    ):
        raise ValueError("The bot must be a channel administrator with permission to post.")
    user_member = await context.bot.get_chat_member(chat.id, user_id)
    if user_member.status not in admin_statuses:
        raise ValueError("You must be a channel administrator to route feeds there.")
    return str(chat.id)


async def _create_configuration(
    update: Update,
    context: CallbackContext,
    settings: Settings,
    repository: FeedRepository,
    name: str,
    url: str,
    channel: str,
    interval: int,
) -> None:
    try:
        name = _configuration_name(name)
        if not _valid_url(url):
            raise ValueError("Feed URL must start with http:// or https://.")
        minutes = _interval(str(interval), settings)
        channel_id = await _resolve_channel(context, channel, update.effective_user.id)
        configuration = await repository.create_configuration(
            user_id=update.effective_user.id,
            name=name,
            url=url.strip(),
            channel_id=channel_id,
            interval_minutes=minutes,
        )
    except DuplicateConfigurationError:
        await update.effective_message.reply_text(
            "You already have a configuration with that name. Choose a different name."
        )
        return
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    except Exception:
        logger.exception("Could not add feed configuration")
        await update.effective_message.reply_text(
            "I could not access that channel. Make sure I am an administrator in it and try again."
        )
        return
    await update.effective_message.reply_text(
        f"Configuration <b>#{configuration.id} · {escape(configuration.name)}</b> added "
        f"for channel <code>{escape(configuration.channel_id)}</code>.\n"
        f"I will check the shared feed at least every {configuration.interval_minutes} minutes.",
        parse_mode="HTML",
    )


async def add_start(update: Update, context: CallbackContext) -> int:
    settings: Settings = context.application.bot_data["settings"]
    if not await _guard(update, settings):
        return ConversationHandler.END
    if context.args:
        if len(context.args) not in {3, 4}:
            await update.effective_message.reply_text(
                "Usage: /addfeed NAME URL CHANNEL [MINUTES]"
            )
            return ConversationHandler.END
        interval = context.args[3] if len(context.args) == 4 else str(settings.default_interval_minutes)
        try:
            minutes = _interval(interval, settings)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return ConversationHandler.END
        await _create_configuration(
            update,
            context,
            settings,
            context.application.bot_data["repository"],
            context.args[0],
            context.args[1],
            context.args[2],
            minutes,
        )
        return ConversationHandler.END
    context.user_data["new_configuration"] = {}
    await update.effective_message.reply_text(
        "Send a name for this configuration, or /cancel to stop."
    )
    return ADD_NAME


async def add_name(update: Update, context: CallbackContext) -> int:
    try:
        context.user_data["new_configuration"]["name"] = _configuration_name(
            update.effective_message.text
        )
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return ADD_NAME
    await update.effective_message.reply_text("Send the RSS/Atom feed URL.")
    return ADD_URL


async def add_url(update: Update, context: CallbackContext) -> int:
    value = update.effective_message.text.strip()
    if not _valid_url(value):
        await update.effective_message.reply_text("Please send a valid http(s) feed URL.")
        return ADD_URL
    context.user_data["new_configuration"]["url"] = value
    await update.effective_message.reply_text(
        "Now send the channel username (for example @news) or its numeric channel ID.\n"
        "The bot must be an administrator in that channel."
    )
    return ADD_CHANNEL


async def add_channel(update: Update, context: CallbackContext) -> int:
    settings: Settings = context.application.bot_data["settings"]
    try:
        context.user_data["new_configuration"]["channel"] = await _resolve_channel(
            context, update.effective_message.text, update.effective_user.id
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
    values = context.user_data.pop("new_configuration", {})
    await _create_configuration(
        update,
        context,
        settings,
        context.application.bot_data["repository"],
        values["name"],
        values["url"],
        values["channel"],
        minutes,
    )
    return ConversationHandler.END


async def edit_start(update: Update, context: CallbackContext) -> int:
    settings: Settings = context.application.bot_data["settings"]
    if not await _guard(update, settings):
        return ConversationHandler.END
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /editfeed <id>")
        return ConversationHandler.END
    configuration_id = int(context.args[0])
    configuration = await context.application.bot_data["repository"].get_configuration(
        configuration_id, update.effective_user.id
    )
    if configuration is None:
        await update.effective_message.reply_text("Configuration not found.")
        return ConversationHandler.END
    context.user_data["edit_configuration_id"] = configuration_id
    await update.effective_message.reply_text(
        "Which field should change? Reply with one of: name, url, channel, interval, enabled."
    )
    return EDIT_FIELD


async def edit_field(update: Update, context: CallbackContext) -> int:
    field = update.effective_message.text.strip().lower()
    if field not in {"name", "url", "channel", "interval", "enabled"}:
        await update.effective_message.reply_text(
            "Choose name, url, channel, interval, or enabled."
        )
        return EDIT_FIELD
    context.user_data["edit_field"] = field
    await update.effective_message.reply_text(f"Send the new value for {field}.")
    return EDIT_VALUE


async def edit_value(update: Update, context: CallbackContext) -> int:
    settings: Settings = context.application.bot_data["settings"]
    repository: FeedRepository = context.application.bot_data["repository"]
    field = context.user_data.pop("edit_field")
    configuration_id = context.user_data.pop("edit_configuration_id")
    value = update.effective_message.text.strip()
    try:
        if field == "name":
            values = {"name": _configuration_name(value)}
        elif field == "url":
            if not _valid_url(value):
                raise ValueError("Feed URL must start with http:// or https://.")
            values = {"url": value}
        elif field == "channel":
            values = {
                "channel_id": await _resolve_channel(
                    context, value, update.effective_user.id
                )
            }
        elif field == "interval":
            values = {"interval_minutes": _interval(value, settings)}
        else:
            if value.lower() not in {"on", "off", "true", "false", "yes", "no"}:
                raise ValueError("Enabled must be on or off.")
            values = {"enabled": value.lower() in {"on", "true", "yes"}}
        configuration = await repository.update_configuration(
            configuration_id, update.effective_user.id, **values
        )
    except DuplicateConfigurationError:
        await update.effective_message.reply_text(
            "You already have a configuration with that name."
        )
        return ConversationHandler.END
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return ConversationHandler.END
    except Exception:
        logger.exception("Could not edit configuration %s", configuration_id)
        await update.effective_message.reply_text(
            "I could not update that configuration. Check the channel access and try again."
        )
        return ConversationHandler.END
    if configuration is None:
        await update.effective_message.reply_text("Configuration not found.")
    else:
        await update.effective_message.reply_text(
            f"Configuration #{configuration_id} updated."
        )
    return ConversationHandler.END


async def cancel(update: Update, context: CallbackContext) -> int:
    context.user_data.pop("new_configuration", None)
    context.user_data.pop("edit_configuration_id", None)
    context.user_data.pop("edit_field", None)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


async def list_feeds(update: Update, context: CallbackContext) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await _guard(update, settings):
        return
    user_id = update.effective_user.id
    repository: FeedRepository = context.application.bot_data["repository"]
    configurations = await repository.list_configurations(user_id)
    if not configurations:
        await update.effective_message.reply_text(
            "No feed configurations yet. Use /addfeed to add one."
        )
        return
    lines = ["<b>Your feed configurations</b>"]
    delivery_errors = await repository.configuration_errors(user_id)
    for configuration in configurations:
        status = "enabled" if configuration.enabled else "paused"
        error_messages = [configuration.source.last_error, delivery_errors.get(configuration.id)]
        error = "".join(
            f"\n   ⚠️ {escape(message[:180])}"
            for message in error_messages
            if message
        )
        lines.append(
            f"\n<b>#{configuration.id} · {escape(configuration.name)}</b> · {status} · "
            f"every {configuration.interval_minutes} min\n"
            f"<code>{escape(configuration.channel_id)}</code> · "
            f"<a href=\"{escape(configuration.source.url, quote=True)}\">feed</a>{error}"
        )
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
    )


async def delete_start(update: Update, context: CallbackContext) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await _guard(update, settings):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /deletefeed <id>")
        return
    configuration_id = int(context.args[0])
    configuration = await context.application.bot_data["repository"].get_configuration(
        configuration_id, update.effective_user.id
    )
    if configuration is None:
        await update.effective_message.reply_text("Configuration not found.")
        return
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Delete", callback_data=f"delete:confirm:{configuration_id}"),
            InlineKeyboardButton("Cancel", callback_data=f"delete:cancel:{configuration_id}"),
        ]]
    )
    await update.effective_message.reply_text(
        f"Delete configuration #{configuration_id} ({escape(configuration.name)})?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def delete_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if not _has_private_user(update):
        await query.answer("Open the bot in a private chat to manage configurations.", show_alert=True)
        return
    await query.answer()
    action, decision, raw_id = query.data.split(":")
    if action != "delete":
        return
    if not raw_id.isdigit():
        await query.edit_message_text("Invalid configuration ID.")
        return
    configuration_id = int(raw_id)
    repository: FeedRepository = context.application.bot_data["repository"]
    if decision == "confirm":
        deleted = await repository.delete_configuration(
            configuration_id, update.effective_user.id
        )
        await query.edit_message_text(
            "Configuration deleted." if deleted else "Configuration was already deleted."
        )
    else:
        await query.edit_message_text("Deletion cancelled.")


async def toggle_feed(update: Update, context: CallbackContext) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await _guard(update, settings):
        return
    if (
        len(context.args) != 2
        or not context.args[0].isdigit()
        or context.args[1].lower() not in {"on", "off"}
    ):
        await update.effective_message.reply_text("Usage: /togglefeed <id> on|off")
        return
    configuration_id = int(context.args[0])
    configuration = await context.application.bot_data["repository"].update_configuration(
        configuration_id,
        update.effective_user.id,
        enabled=context.args[1].lower() == "on",
    )
    await update.effective_message.reply_text(
        f"Configuration #{configuration_id} is now "
        f"{'enabled' if configuration and configuration.enabled else 'paused'}."
        if configuration
        else "Configuration not found."
    )


async def stats(update: Update, context: CallbackContext) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await _guard(update, settings):
        return
    configurations, enabled, posted = await context.application.bot_data[
        "repository"
    ].user_stats(update.effective_user.id)
    await update.effective_message.reply_text(
        f"Your configurations: {configurations} ({enabled} enabled)\n"
        f"Articles posted to your channels: {posted}"
    )


async def help_command(update: Update, context: CallbackContext) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await _guard(update, settings):
        return
    await update.effective_message.reply_text(
        "<b>RSS-to-Telegram</b>\n\n"
        "/addfeed — add a configuration interactively\n"
        "/addfeed NAME URL CHANNEL [MINUTES] — add directly\n"
        "/feeds — list your configurations\n"
        "/editfeed ID — update a configuration\n"
        "/togglefeed ID on|off — pause or resume\n"
        "/deletefeed ID — delete with confirmation\n"
        "/stats — show your counters\n"
        "/cancel — cancel an active prompt\n\n"
        "Use commands in a private chat. Configurations with the same feed URL share one poll.",
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
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
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
