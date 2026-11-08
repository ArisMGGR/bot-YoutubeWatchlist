"""YtWatchlistBot — a single always-on Telegram bot.

One long-running process does everything (same shape as the Reminder Bot):

  * a background loop checks your channels every few minutes and sends a
    Telegram message for each new video / livestream, with 💾 Save and 🗑 Delete
    buttons;
  * the same process handles those button taps and the chat commands
    (/start, /check, /except, /authorize, /help) in real time.

There's no daily cron and no separate listener — just run this and leave it
running (systemd unit in `manual-setup/deploy/`).

Prefer Discord? Set `CHAT_PLATFORM=discord` (and `DISCORD_BOT_TOKEN`) in
conf.env and this same script hands over to `discordbot.py`, which offers the
same features. The check itself lives in `checker.py` and is shared by both.

Setup (one time):
    1. Message @BotFather, send /newbot, copy the token.
    2. Put it in a `conf.env` next to this script (copy `manual-setup/conf.env.example`):
           TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
       (a TELEGRAM_BOT_TOKEN env var or a `token.txt` file also work.)
    3. pip install -r requirements.txt
    4. Connect YouTube (`python manual-setup/authorize_youtube.py`) — your subscriptions are
       the watchlist. Optionally opt out of some in `exceptions.txt`.
    5. python ytwatchlistbot.py
    6. Open Telegram, find your bot, send /start so it knows where to notify.
"""
import asyncio
import html
import logging

import config

# The hand-off to Discord has to happen *before* the telegram imports below: a
# Discord-only install has no python-telegram-bot to import, so reaching this
# file's main() would already be too late.
if __name__ == "__main__" and config.CHAT_PLATFORM == "discord":
    import discordbot

    raise SystemExit(discordbot.main())

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import (
    BadRequest,
    Conflict,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import archive as archive_mod
import checker
import discovery
import logging_setup
import youtube_auth
import youtube_playlist

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
# callback_data prefixes (callback_data is capped at 64 bytes; a video_id is 11).
CB_SAVE = "sv:"
CB_DELETE = "dl:"
CB_NOOP = "noop"
CB_EXCEPT = "exc:"  # /except type buttons
CB_AUTH = "auth:"   # /authorize buttons: auth:yes | auth:no | auth:cancel

# The Google sign-in tip shown by /authorize (same advice as manual-setup/authorize_youtube.py).
_AUTH_TIP = (
    "Sign in with the Google account whose subscriptions you want watched. If "
    "it owns several channels, Google shows a <i>Choose a channel</i> screen — "
    "pick the right one; saves go there too."
)

# /except conversation states.
EXC_CHANNEL, EXC_TYPE = range(2)

# Commands and typed answers are taken from new messages only. Handlers match
# edited messages too by default, and those carry no `update.message`: editing
# a sent "/chek" into "/help" would crash the handler instead of running it.
NEW_MESSAGE = filters.UpdateType.MESSAGE

# Button choice -> exception rule (values from discovery).
_EXC_RULES = {
    "mute": discovery.MUTE,
    "videos": discovery.ONLY_VIDEOS,
    "livestreams": discovery.ONLY_LIVE,
}
_EXC_DESC = {
    discovery.MUTE: "muted — no notifications",
    discovery.ONLY_VIDEOS: "only regular videos (livestreams muted)",
    discovery.ONLY_LIVE: "only livestreams (regular videos muted)",
}


# --------------------------------------------------------------------------- #
# Logging (mirrors the reminder bot: one readable line per event)
# --------------------------------------------------------------------------- #
logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
for noisy in ("httpx", "httpcore", "telegram.ext.Updater"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
# Mirror every log line into a per-day file under logs/ (see logging_setup.py).
logging_setup.setup_file_logging()
log = logging.getLogger("ytwatchlistbot")


# --------------------------------------------------------------------------- #
# Where to send notifications
# --------------------------------------------------------------------------- #
def _notify_chat_id(context_or_app):
    """The chat we notify. Configured TELEGRAM_CHAT_ID wins; otherwise the chat
    that first sent /start (learned at runtime, persisted in the archive).

    Accepts either an Application or a handler Context — both expose `bot_data`.
    """
    return context_or_app.bot_data.get("notify_chat_id")


def _authorized(context, chat_id):
    """Serve only the notify chat. If none is known yet (no TELEGRAM_CHAT_ID and
    nobody has sent /start), allow it so the owner's first /start can register.

    Every handler — including /start and /help — goes through this, so a
    stranger who finds the bot can neither use it nor re-point notifications
    to their own chat."""
    target = _notify_chat_id(context)
    return target is None or str(chat_id) == str(target)


async def _may_control(update, context):
    """May whoever sent this update use the bot? It must come from the notify
    chat (see :func:`_authorized`). In a private chat that's the owner. In a
    group or channel, every member reads the notifications and can tap the
    buttons, so the chat alone isn't enough: it takes an admin of that chat,
    as Discord takes Manage Server."""
    chat = update.effective_chat
    if not _authorized(context, chat.id):
        return False
    if chat.type == ChatType.PRIVATE:
        return True
    message = update.message
    if message is not None and getattr(message.sender_chat, "id", None) == chat.id:
        return True  # an anonymous admin, posting as the group itself
    user = update.effective_user
    if user is None:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError as exc:
        log.warning("Couldn't check whether user %s is an admin: %s", user.id, exc)
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


# Shown to anyone who isn't the owner's chat. Deliberately says nothing about
# who the owner is or how the chat was configured.
_NOT_YOUR_CHAT = (
    "This bot is already paired with another chat. To move it, set "
    "TELEGRAM_CHAT_ID in conf.env (or clear notify_chat_id in archive.json) "
    "and restart it."
)


# --------------------------------------------------------------------------- #
# Message building
# --------------------------------------------------------------------------- #
def build_video_text(v):
    icon, label = checker.TYPE_META.get(
        v.get("type", "video"), checker.TYPE_META["video"]
    )
    title = html.escape(v.get("title") or "")
    channel = html.escape(v.get("channel_title") or v.get("author") or "")

    lines = [f"{icon} <b>{label}</b>"]
    if channel:
        lines.append(f"👤 {channel}")
    if title:
        lines.append(f"<b>{title}</b>")
    published = checker.fmt_published(v.get("published"))
    if published:
        lines.append(f"📅 Released: {published}")
    if v.get("scheduled_start"):
        lines.append(f"🕒 Starts: {v['scheduled_start']}")
    lines.append(v["link"])
    return "\n".join(lines)


def _video_keyboard(video_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💾 Save", callback_data=f"{CB_SAVE}{video_id}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"{CB_DELETE}{video_id}"),
            ]
        ]
    )


async def send_video(bot, chat_id, v):
    """Send one notification. Keeps the link preview so the thumbnail shows."""
    await bot.send_message(
        chat_id=chat_id,
        text=build_video_text(v),
        parse_mode=ParseMode.HTML,
        reply_markup=_video_keyboard(v["video_id"]),
        disable_web_page_preview=False,
    )


# --------------------------------------------------------------------------- #
# The periodic check, wired to Telegram
# --------------------------------------------------------------------------- #
class TelegramNotifier:
    """checker.Notifier for Telegram: how each kind of message is phrased here."""

    def __init__(self, application: Application):
        self.application = application

    @property
    def chat_id(self):
        return _notify_chat_id(self.application)

    @property
    def chat_known(self):
        return self.chat_id is not None

    async def _send(self, text):
        await self.application.bot.send_message(
            self.chat_id, text, parse_mode=ParseMode.HTML
        )

    async def send_video(self, v):
        await send_video(self.application.bot, self.chat_id, v)

    async def send_first_run(self, watched):
        await self._send(
            "✅ <b>YtWatchlistBot is set up!</b>\n"
            f"Watching <b>{watched}</b> channel(s). I'll message you here "
            "whenever a new video or livestream drops."
        )

    async def send_seeded(self, names):
        listing = checker.seeded_listing(names, html.escape)
        await self._send(
            f"👀 Now watching {len(names)} new channel(s):\n{listing}"
        )

    async def send_auth_alert(self, reason):
        await self._send(
            "🔑 <b>YouTube needs re-authorizing</b>\n"
            f"{html.escape(reason)}\n\n"
            "I can't read your subscriptions, so checks are paused. Send "
            "/authorize and I'll walk you through reconnecting right here."
        )

    def describe_error(self, exc):
        return describe_error(exc)


def _check_state(application: Application):
    """Per-process state the shared checker keeps between runs."""
    return application.bot_data.setdefault("check_state", {})


async def run_check(application: Application, *, trigger="scheduled"):
    """Run one check, notifying through Telegram."""
    await checker.run_check(
        TelegramNotifier(application), _check_state(application), trigger=trigger
    )


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # Only the first /start (or the pinned chat) registers: once a notify chat
    # is known, a /start from anywhere else must not be able to take it over.
    if not _authorized(context, chat_id):
        await update.message.reply_text(_NOT_YOUR_CHAT)
        return
    # In a group, only an admin may register it (or see the how-to).
    if not await _may_control(update, context):
        return
    # Learn where to notify if TELEGRAM_CHAT_ID wasn't configured.
    if not config.TELEGRAM_CHAT_ID:
        context.application.bot_data["notify_chat_id"] = str(chat_id)
        await asyncio.to_thread(_remember_chat, str(chat_id))
    await update.message.reply_text(
        "👋 I ping you when a watched channel posts a new video or livestream.\n\n"
        f"On each message: tap 💾 Save to add it to your '{config.YOUTUBE_PLAYLIST_NAME}' "
        "playlist on YouTube, or 🗑 Delete to remove the message.\n\n"
        "Commands:\n"
        "• /check — check the channels right now\n"
        "• /except — mute a channel or limit it to videos / livestreams\n"
        "• /authorize — connect (or reconnect) your YouTube account\n"
        "• /help — how it works"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _may_control(update, context):
        return
    await update.message.reply_text(
        "<b>How I work</b>\n\n"
        f"Every {config.CHECK_INTERVAL_MINUTES} minute(s) I read the RSS feed of "
        "each channel you're <b>subscribed to on YouTube</b> and message you "
        "about any new video or livestream (Shorts are skipped). Opt out of "
        "channels or types in <code>exceptions.txt</code>.\n\n"
        f"• 💾 <b>Save</b> adds the video to your '{html.escape(config.YOUTUBE_PLAYLIST_NAME)}' "
        "playlist on YouTube (idempotent).\n"
        "• 🗑 <b>Delete</b> removes that message.\n"
        "• /check runs a check immediately instead of waiting.\n"
        "• /except adds an opt-out: I'll ask for the channel, then you pick "
        "🔕 mute / ▶️ only videos / 📺 only livestreams.\n"
        "• /authorize connects your YouTube account from here (no server "
        "access needed): I show a code, you enter it on google.com/device.",
        parse_mode=ParseMode.HTML,
    )


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _may_control(update, context):
        return
    if checker.check_lock().locked():
        await update.message.reply_text("⏳ A check is already running…")
        return
    await update.message.reply_text("🔎 Checking your channels now…")
    # In the background: python-telegram-bot handles one update at a time, so
    # awaiting the check here would leave every tap and command unanswered
    # until a pass over all the feeds finished. A plain asyncio task, not
    # application.create_task: Application.stop() waits for those, so stopping
    # the bot mid-check would wait for the whole pass (the periodic check
    # isn't waited for either). Kept in bot_data so it isn't garbage-collected.
    context.application.bot_data["check_task"] = asyncio.create_task(
        _manual_check(context.application, update.message)
    )


async def _manual_check(application: Application, message):
    """Run a /check; if it fails, log why and say so in the chat."""
    try:
        await run_check(application, trigger="manual")
    except Exception as exc:  # noqa: BLE001 - the /check must be answered
        friendly = describe_error(exc)
        if friendly:
            log.warning("During /check: %s", friendly)
        else:
            log.error("Unexpected error during /check:", exc_info=exc)
        try:
            await message.reply_text("😵 The check failed — see the log.")
        except TelegramError as reply_exc:
            log.warning("Couldn't report the failed /check: %s", reply_exc)


# --------------------------------------------------------------------------- #
# /except — add a per-channel opt-out from Telegram (a guided conversation)
# --------------------------------------------------------------------------- #
def _exception_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔕 Mute the channel", callback_data=f"{CB_EXCEPT}mute")],
            [
                InlineKeyboardButton("▶️ Only videos", callback_data=f"{CB_EXCEPT}videos"),
                InlineKeyboardButton("📺 Only livestreams", callback_data=f"{CB_EXCEPT}livestreams"),
            ],
            [InlineKeyboardButton("✖️ Cancel", callback_data=f"{CB_EXCEPT}cancel")],
        ]
    )


async def except_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /except — ask which channel to add an exception for."""
    if not await _may_control(update, context):
        return ConversationHandler.END
    context.user_data.pop("exc_channel", None)  # a restarted /except starts clean
    if not youtube_playlist.is_configured():
        await update.message.reply_text(
            "⚠️ Connect YouTube first (run `python manual-setup/authorize_youtube.py`). I read "
            "your subscriptions to know which channels you can add exceptions for."
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "🔧 Which channel? Send me its name, @handle, channel URL, or UC… id.\n"
        "Send /cancel to stop."
    )
    return EXC_CHANNEL


async def except_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Got a channel name — match it to a subscription and offer the type buttons."""
    text = (update.message.text or "").strip()
    try:
        matches = await asyncio.to_thread(discovery.lookup_subscription, text)
    except youtube_playlist.NotAuthorized as exc:
        await update.message.reply_text(f"⚠️ {exc}")
        return ConversationHandler.END
    except Exception as exc:  # noqa: BLE001 - surface a friendly error, don't crash
        log.warning("Subscription lookup failed: %s", exc)
        await update.message.reply_text(
            "😵 Couldn't read your subscriptions right now — try again in a moment."
        )
        return ConversationHandler.END

    if not matches:
        await update.message.reply_text(
            f"❌ I couldn't find “{html.escape(text)}” in your subscriptions. "
            "Check the spelling, or make sure you're subscribed to it, then run "
            "/except again.",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    if len(matches) > 1:
        listing = "\n".join(f"• {html.escape(m['title'])}" for m in matches[:10])
        more = "\n…" if len(matches) > 10 else ""
        await update.message.reply_text(
            f"🔎 That matches more than one subscription:\n{listing}{more}\n\n"
            "Run /except again with a more specific name or the @handle.",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    channel = matches[0]
    context.user_data["exc_channel"] = channel
    await update.message.reply_text(
        f"Channel: <b>{html.escape(channel['title'])}</b>\n"
        "What do you want to opt out of?",
        parse_mode=ParseMode.HTML,
        reply_markup=_exception_keyboard(),
    )
    return EXC_TYPE


async def except_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A type button was tapped — write the rule to exceptions.txt."""
    query = update.callback_query
    await query.answer()
    choice = (query.data or "")[len(CB_EXCEPT):]
    channel = context.user_data.pop("exc_channel", None)

    if choice == "cancel" or not channel:
        await query.edit_message_text("Cancelled — no changes made.")
        return ConversationHandler.END

    rule = _EXC_RULES.get(choice)
    if rule is None:
        await query.edit_message_text("Unknown option — nothing changed.")
        return ConversationHandler.END

    try:
        outcome = await asyncio.to_thread(
            discovery.set_exception,
            config.EXCEPTIONS_FILE, channel["id"], rule, channel["title"],
        )
    except Exception as exc:  # noqa: BLE001 - never fail silently
        log.warning("Couldn't write exception for %s: %s", channel["id"], exc)
        await query.edit_message_text(
            "😵 Couldn't save that exception — please try again."
        )
        return ConversationHandler.END

    verb = "Updated" if outcome == "updated" else "Added"
    await query.edit_message_text(
        f"✅ {verb}: <b>{html.escape(channel['title'])}</b> → {_EXC_DESC[rule]}.\n"
        "Takes effect on the next check.",
        parse_mode=ParseMode.HTML,
    )
    log.info("Exception %s: %s (%s) -> %s", outcome, channel["title"], channel["id"], rule)
    return ConversationHandler.END


async def except_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel fallback for the /except conversation."""
    context.user_data.pop("exc_channel", None)
    await update.message.reply_text("Cancelled — no changes made.")
    return ConversationHandler.END


def except_conversation():
    """/except as a short guided conversation: ask for the channel, then show
    opt-out buttons.

    allow_reentry: a conversation left hanging (buttons never tapped) would
    otherwise swallow every later /except without a reply until /cancel.
    """
    return ConversationHandler(
        entry_points=[CommandHandler("except", except_start, filters=NEW_MESSAGE)],
        states={
            EXC_CHANNEL: [
                MessageHandler(
                    NEW_MESSAGE & filters.TEXT & ~filters.COMMAND, except_channel
                )
            ],
            EXC_TYPE: [
                CallbackQueryHandler(except_type, pattern=f"^{CB_EXCEPT}")
            ],
        },
        fallbacks=[CommandHandler("cancel", except_cancel, filters=NEW_MESSAGE)],
        allow_reentry=True,
    )


# --------------------------------------------------------------------------- #
# /authorize — connect YouTube from the chat (Google's device flow)
# --------------------------------------------------------------------------- #
def _auth_task(application):
    """The running /authorize wait, if any (kept in bot_data)."""
    return application.bot_data.get("auth_task")


async def authorize_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _may_control(update, context):
        return
    app = context.application
    if _auth_task(app) is not None and not _auth_task(app).done():
        await update.message.reply_text(
            "⏳ I'm already waiting for you to approve a code — finish that one, "
            "or tap ✖️ Cancel on it first."
        )
        return

    if youtube_playlist.is_configured():
        # Already connected (or connected with a dead token): confirm first.
        try:
            channel = await asyncio.to_thread(youtube_playlist.acting_channel)
            current = (
                f"YouTube is currently connected as <b>{html.escape(channel['title'])}</b>."
                if channel else "YouTube is connected, but that account has no channel."
            )
        except youtube_playlist.NotAuthorized as exc:
            current = f"The current YouTube connection is broken: {html.escape(str(exc))}"
        except Exception as exc:  # noqa: BLE001 - the name is a nicety only
            log.warning("Couldn't read the connected channel: %s", exc)
            current = "YouTube is currently connected (couldn't read which channel)."
        await update.message.reply_text(
            f"{current}\nReconnect anyway? The new account's subscriptions become "
            "the watchlist and its playlist gets the saves.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔑 Yes, reconnect", callback_data=f"{CB_AUTH}yes"),
                InlineKeyboardButton("✖️ Keep it", callback_data=f"{CB_AUTH}no"),
            ]]),
        )
        return

    await _begin_authorization(app, update.effective_chat.id)


async def _begin_authorization(application: Application, chat_id, edit=None):
    """Start the device flow and post the code. `edit` (an edit_message_text
    coroutine function) replaces the confirmation prompt instead of sending a
    new message."""
    bot = application.bot

    async def say(text, reply_markup=None):
        if edit is not None:
            return await edit(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return await bot.send_message(
            chat_id, text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )

    try:
        flow = await asyncio.to_thread(youtube_auth.start)
    except youtube_auth.DeviceAuthError as exc:  # includes ClientRejected
        await say(f"❌ Can't start authorization: {html.escape(str(exc))}")
        return
    except Exception as exc:  # noqa: BLE001 - never leave the user without an answer
        log.error("Starting device authorization failed:", exc_info=exc)
        await say("😵 Something went wrong starting the authorization — see the log.")
        return

    minutes = int(flow.seconds_left // 60)
    message = await say(
        "🔑 <b>Connect YouTube</b>\n\n"
        f"1. On any device, open: {html.escape(flow.verification_url)}\n"
        f"2. Enter this code: <code>{html.escape(flow.user_code)}</code>\n\n"
        f"{_AUTH_TIP}\n\n"
        f"⏳ Waiting for you to approve… (the code is valid for about {minutes} min)",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("✖️ Cancel", callback_data=f"{CB_AUTH}cancel")]]
        ),
    )
    # A plain asyncio task, not application.create_task: Application.stop()
    # waits for those, so shutting down with a code still pending would hang
    # until it expired (~30 minutes).
    task = asyncio.create_task(
        _wait_for_approval(application, chat_id, message.message_id, flow)
    )
    task.add_done_callback(_log_auth_task_error)
    application.bot_data["auth_task"] = task


def _log_auth_task_error(task):
    """Log what the /authorize wait raised; nothing else would see it."""
    if not task.cancelled() and task.exception() is not None:
        log.error("The /authorize wait failed:", exc_info=task.exception())


async def _wait_for_approval(application: Application, chat_id, message_id, flow):
    """Poll Google until you approve, then save the token and run a check."""
    bot = application.bot

    async def finish(text):
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.HTML
            )
        except TelegramError as exc:
            log.warning("Couldn't update the /authorize message: %s", exc)

    try:
        while True:
            await asyncio.sleep(flow.interval)
            creds = await asyncio.to_thread(youtube_auth.poll, flow)
            if creds is not None:
                break
    except asyncio.CancelledError:
        return  # the Cancel button already answered
    except youtube_auth.DeviceAuthError as exc:
        await finish(f"❌ {html.escape(str(exc))}\nSend /authorize to try again.")
        return
    finally:
        application.bot_data.pop("auth_task", None)

    try:
        await asyncio.to_thread(youtube_auth.save, creds)
    except OSError as exc:
        log.error("Couldn't write the YouTube token file: %s", exc)
        await finish("😵 Google approved it, but I couldn't save the token — see the log.")
        return
    if not creds.refresh_token:
        log.warning("Google didn't return a refresh token; re-authorization may be needed later.")

    try:
        channel = await asyncio.to_thread(youtube_playlist.acting_channel)
        who = (
            f"<b>{html.escape(channel['title'])}</b>" if channel
            else "an account with no YouTube channel"
        )
    except Exception as exc:  # noqa: BLE001 - confirmation only
        log.warning("Couldn't read the connected channel: %s", exc)
        who = "your account"
    log.info("YouTube connected via /authorize (%s).", who)
    await finish(
        f"✅ <b>YouTube connected</b> as {who}.\n"
        "Your subscriptions are now the watchlist and 💾 Save goes to "
        f"'{html.escape(config.YOUTUBE_PLAYLIST_NAME)}' there. Checking now…"
    )
    await run_check(application, trigger="manual")


async def on_auth_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Yes / Keep it / Cancel taps from /authorize."""
    query = update.callback_query
    if not await _may_control(update, context):
        await query.answer()
        return
    await query.answer()
    choice = (query.data or "")[len(CB_AUTH):]
    app = context.application

    if choice == "yes":
        if _auth_task(app) is not None and not _auth_task(app).done():
            await query.edit_message_text(
                "⏳ I'm already waiting for you to approve a code — finish that "
                "one, or tap ✖️ Cancel on it first."
            )
            return
        await _begin_authorization(app, update.effective_chat.id, edit=query.edit_message_text)
    elif choice == "cancel":
        task = app.bot_data.pop("auth_task", None)
        if task is not None:
            task.cancel()
        await query.edit_message_text("✖️ Cancelled — nothing was changed.")
    else:  # "no" or anything else
        await query.edit_message_text("👍 Kept the current YouTube connection.")


# --------------------------------------------------------------------------- #
# Button handler
# --------------------------------------------------------------------------- #
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _may_control(update, context):
        await query.answer()
        return

    data = query.data or ""

    if data.startswith(CB_SAVE):
        video_id = data[len(CB_SAVE):]
        try:
            result = await asyncio.to_thread(youtube_playlist.add_video, video_id)
        except youtube_playlist.NotAuthorized as exc:
            log.warning("Save blocked — YouTube not connected: %s", exc)
            await query.answer(f"⚠️ {exc}", show_alert=True)
            return
        except Exception as exc:  # noqa: BLE001 - never fail a Save silently
            log.warning("Failed to add %s to YouTube: %s", video_id, exc)
            await query.answer(
                "⚠️ Couldn't add it to YouTube — please try again", show_alert=True
            )
            return
        await query.answer(
            f"💾 Added to your '{config.YOUTUBE_PLAYLIST_NAME}' playlist"
            if result == "added"
            else "Already in your YouTube playlist"
        )
        # Reflect the saved state, keeping Delete available.
        try:
            await query.edit_message_reply_markup(
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("✅ Saved", callback_data=CB_NOOP),
                            InlineKeyboardButton(
                                "🗑 Delete", callback_data=f"{CB_DELETE}{video_id}"
                            ),
                        ]
                    ]
                )
            )
        except BadRequest:
            pass  # message too old to edit — the save still happened

    elif data.startswith(CB_DELETE):
        try:
            await query.delete_message()
            await query.answer("🗑 Deleted")
        except TelegramError:
            # Bots can't delete messages older than 48h.
            await query.answer(
                "Couldn't delete — message too old", show_alert=True
            )

    else:  # CB_NOOP or anything unrecognised
        await query.answer()


# --------------------------------------------------------------------------- #
# Error handling (mirrors the reminder bot)
# --------------------------------------------------------------------------- #
def describe_error(error: Exception) -> str:
    """Turn an exception into one short, human-readable line (or '' if unknown)."""
    if isinstance(error, Conflict):
        return (
            "⚠️  Another copy of this bot is running with the same token "
            "(getUpdates conflict). Stop the other instance — only one can poll."
        )
    if isinstance(error, InvalidToken):
        return (
            "❌ Telegram rejected the bot token. Check TELEGRAM_BOT_TOKEN in your "
            "conf.env (get a fresh token from @BotFather if needed)."
        )
    if isinstance(error, Forbidden):
        return (
            "🚫 Telegram refused a message (Forbidden). Usually the user blocked "
            "the bot or never sent it /start."
        )
    if isinstance(error, RetryAfter):
        return (
            f"⏳ Telegram is rate-limiting us — asked to wait {error.retry_after}s. "
            "The library retries automatically; no action needed."
        )
    if isinstance(error, TimedOut):
        return (
            "🌐 Request to Telegram timed out. Temporary network slowness — the "
            "bot keeps retrying automatically; no action needed."
        )
    if isinstance(error, BadRequest):
        # Must precede NetworkError (BadRequest subclasses it).
        return (
            f"⚠️  Telegram rejected a request we sent (BadRequest: {error}). "
            "Often unsupported characters/formatting in a message."
        )
    if isinstance(error, NetworkError):
        detail = str(error).strip().rstrip(":") or error.__class__.__name__
        return (
            f"🌐 Network hiccup while talking to Telegram ({detail}). "
            "Temporary connection loss — the bot reconnects automatically."
        )
    return ""


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    friendly = describe_error(context.error)
    if friendly:
        # A BadRequest usually means we sent something malformed, so it keeps
        # its traceback (as on Discord) to show which message it was.
        bad_request = isinstance(context.error, BadRequest)
        log.warning(friendly, exc_info=context.error if bad_request else None)
        return
    log.error("Unexpected error while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "😵 Sorry, something went wrong handling that. Please try again."
            )
        except NetworkError:
            pass


# --------------------------------------------------------------------------- #
# Startup + entry point
# --------------------------------------------------------------------------- #
def _remember_chat(chat_id):
    """Persist a runtime-learned notify chat into the archive file."""
    archive_mod.set_keys(config.ARCHIVE_FILE, notify_chat_id=chat_id)


async def post_init(application: Application):
    # Resolve where to notify: configured value wins, else whatever /start
    # last registered (persisted in the archive).
    stored = archive_mod.load(config.ARCHIVE_FILE).get("notify_chat_id")
    application.bot_data["notify_chat_id"] = config.TELEGRAM_CHAT_ID or stored
    if not youtube_playlist.is_configured():
        log.warning(
            "YouTube isn't connected yet — 💾 Save won't work until you run "
            "`python manual-setup/authorize_youtube.py` (see the README)."
        )
    else:
        try:
            channel = await asyncio.to_thread(youtube_playlist.acting_channel)
            if channel:
                log.info(
                    "💾 Save targets YouTube channel: %s (%s)",
                    channel["title"], channel["id"],
                )
            else:
                log.warning("Connected YouTube account has no channel.")
        except Exception as exc:  # noqa: BLE001 - startup diagnostic only
            log.warning("Couldn't confirm YouTube channel: %s", exc)
    application.create_task(
        checker.check_loop(TelegramNotifier(application), _check_state(application))
    )


def handlers():
    """Every update handler, in the order they're registered."""
    return [
        CommandHandler("start", start, filters=NEW_MESSAGE),
        CommandHandler("help", help_command, filters=NEW_MESSAGE),
        CommandHandler("check", check_command, filters=NEW_MESSAGE),
        CommandHandler("authorize", authorize_command, filters=NEW_MESSAGE),
        # Before the generic button handler, so the conversation's `exc:`
        # callbacks are handled there while a conversation is active.
        except_conversation(),
        CallbackQueryHandler(on_auth_button, pattern=f"^{CB_AUTH}"),
        CallbackQueryHandler(on_button),
    ]


def main():
    # Running this file directly is handled at the top of the module (it must
    # be, to skip the telegram imports). This covers main() being called from
    # somewhere else, e.g. an import.
    if config.CHAT_PLATFORM == "discord":
        import discordbot

        discordbot.main()
        return

    if not config.TELEGRAM_BOT_TOKEN:
        print(
            "ERROR: No Telegram bot token found.\n\n"
            "Get one from @BotFather (send /newbot), then either:\n"
            "  • put TELEGRAM_BOT_TOKEN=... in a conf.env file next to this script,\n"
            "  • set a TELEGRAM_BOT_TOKEN environment variable, or\n"
            "  • create a token.txt file next to this script with the token.\n\n"
            "Using Discord instead? Set CHAT_PLATFORM=discord and "
            "DISCORD_BOT_TOKEN in conf.env (see the README).\n"
        )
        return

    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    for handler in handlers():
        app.add_handler(handler)
    app.add_error_handler(on_error)

    log.info(
        "YtWatchlistBot is running (checking every %s min). Ctrl+C to stop.",
        config.CHECK_INTERVAL_MINUTES,
    )
    try:
        app.run_polling()
    except (InvalidToken, Conflict) as exc:
        log.error(describe_error(exc))


if __name__ == "__main__":
    main()
