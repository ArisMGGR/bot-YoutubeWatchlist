"""YtWatchlistBot on Discord — the same bot, talking to a Discord channel.

Run this instead of `ytwatchlistbot.py` (or set `CHAT_PLATFORM=discord` in
conf.env and keep running `ytwatchlistbot.py`, which hands over to this module).
Everything except the messaging is shared: `checker` runs the periodic check,
`discovery` finds new videos, `youtube_playlist` does the Save.

What you get in Discord:
  * a message per new video / livestream in one channel, with 💾 Save and
    🗑 Delete buttons;
  * slash commands /start, /check, /except, /authorize and /help.

Setup (one time):
    1. https://discord.com/developers/applications → New Application → Bot,
       then Reset Token and copy it.
    2. Put it in conf.env:  DISCORD_BOT_TOKEN=...  (plus CHAT_PLATFORM=discord)
    3. Invite the bot: OAuth2 → URL Generator → scopes `bot` and
       `applications.commands`, permissions "Send Messages" and "Embed Links"
       (🗑 Delete only removes the bot's own messages, which needs no extra
       permission). Open the generated URL and pick your server.
    4. pip install -r requirements.txt
    5. python ytwatchlistbot.py
    6. In the channel you want notifications in, run /start.

No privileged intents are needed — the bot never reads message content.
"""
import asyncio
import errno
import logging
import socket
import sys
import time

import aiohttp
import discord
from discord import app_commands

import archive as archive_mod
import checker
import config
import discovery
import logging_setup
import youtube_auth
import youtube_playlist

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
# Button custom_ids. Discord sends them back on every click, including after a
# restart, so on_interaction parses them instead of relying on a live View.
CB_SAVE = "ytw:sv:"
CB_DELETE = "ytw:dl:"
CB_SAVED = "ytw:saved"
# /authorize buttons (ephemeral messages can carry buttons too).
CB_AUTH_YES = "ytw:auth:yes"
CB_AUTH_NO = "ytw:auth:no"
CB_AUTH_CANCEL = "ytw:auth:cancel"

# The Google sign-in tip shown by /authorize (same advice as manual-setup/authorize_youtube.py).
_AUTH_TIP = (
    "Sign in with the Google account whose subscriptions you want watched. If "
    "it owns several channels, Google shows a *Choose a channel* screen — pick "
    "the right one; saves go there too."
)

# /except choices -> the rule written to exceptions.txt.
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

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
for noisy in ("discord.client", "discord.gateway", "discord.http"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
# Mirror every log line into a per-day file under logs/ (see logging_setup.py).
logging_setup.setup_file_logging()
log = logging.getLogger("ytwatchlistbot")


# --------------------------------------------------------------------------- #
# Message building
# --------------------------------------------------------------------------- #
def build_video_text(v):
    """The notification body. The bare link at the end is what gives us the
    video embed, so it stays last and unformatted."""
    icon, label = checker.TYPE_META.get(
        v.get("type", "video"), checker.TYPE_META["video"]
    )
    esc = discord.utils.escape_markdown
    title = v.get("title") or ""
    channel = v.get("channel_title") or v.get("author") or ""

    lines = [f"{icon} **{label}**"]
    if channel:
        lines.append(f"👤 {esc(channel)}")
    if title:
        lines.append(f"**{esc(title)}**")
    published = checker.fmt_published(v.get("published"))
    if published:
        lines.append(f"📅 Released: {published}")
    if v.get("scheduled_start"):
        lines.append(f"🕒 Starts: {v['scheduled_start']}")
    lines.append(v["link"])
    return "\n".join(lines)


def _video_view(video_id, *, saved=False):
    """Save / Delete buttons. `timeout=None` so they keep working after a
    restart (the click is matched by custom_id, not by a stored View)."""
    view = discord.ui.View(timeout=None)
    if saved:
        view.add_item(
            discord.ui.Button(
                label="✅ Saved",
                style=discord.ButtonStyle.success,
                custom_id=CB_SAVED,
                disabled=True,
            )
        )
    else:
        view.add_item(
            discord.ui.Button(
                label="💾 Save",
                style=discord.ButtonStyle.primary,
                custom_id=f"{CB_SAVE}{video_id}",
            )
        )
    view.add_item(
        discord.ui.Button(
            label="🗑 Delete",
            style=discord.ButtonStyle.danger,
            custom_id=f"{CB_DELETE}{video_id}",
        )
    )
    return view


# --------------------------------------------------------------------------- #
# Error handling (the Discord half of ytwatchlistbot.describe_error)
# --------------------------------------------------------------------------- #
def describe_error(error: Exception) -> str:
    """Turn an exception into one short, human-readable line (or '' if unknown)."""
    expired = describe_expired_interaction(error)
    if expired:
        return expired
    if isinstance(error, discord.LoginFailure):
        return (
            "❌ Discord rejected the bot token. Check DISCORD_BOT_TOKEN in your "
            "conf.env (reset it in the Developer Portal if needed)."
        )
    if isinstance(error, discord.Forbidden):
        return (
            "🚫 Discord refused the action (Forbidden). The bot is probably "
            "missing a permission in that channel — it needs View Channel, "
            "Send Messages and Embed Links."
        )
    if isinstance(error, discord.NotFound):
        return (
            "❓ Discord couldn't find that channel or message. If the "
            "notification channel was deleted, run /start in the new one."
        )
    if isinstance(error, discord.RateLimited):
        return (
            f"⏳ Discord is rate-limiting us — waiting {error.retry_after:.0f}s. "
            "The library retries automatically; no action needed."
        )
    if isinstance(error, discord.HTTPException):
        # Must come after the subclasses above.
        return f"⚠️  Discord rejected a request we sent ({error})."
    # Only genuine connection failures. A bare OSError is usually local (an
    # archive.json that can't be written), and calling that a dropped
    # connection hid the real cause and its traceback.
    if isinstance(
        error,
        (discord.ConnectionClosed, discord.GatewayNotFound, aiohttp.ClientConnectionError),
    ):
        return (
            f"🌐 Connection to Discord dropped: {describe_connection_failure(error)}. "
            "The client reconnects automatically."
        )
    return ""


# Discord error codes for an interaction we answered too late / twice.
_UNKNOWN_INTERACTION = 10062
_ALREADY_ACKNOWLEDGED = 40060


def describe_expired_interaction(error: Exception) -> str:
    """A click or command we couldn't answer in time, or '' if that's not it.

    Discord gives the bot 3 seconds to acknowledge an interaction; after that
    the token is dead and any reply gets 404 "Unknown interaction" (10062).
    The usual cause is a slow or flaky network on the bot's side, or a click
    delivered late right after a reconnect. Nothing was done for that click,
    so trying again is safe.
    """
    if isinstance(error, discord.NotFound) and error.code == _UNKNOWN_INTERACTION:
        return (
            "⌛ A button click or command expired before I could answer it — "
            "Discord allows 3 seconds and my reply took longer (slow network, "
            "or the click arrived during a reconnect). Nothing was changed; "
            "just try again."
        )
    if isinstance(error, discord.HTTPException) and error.code == _ALREADY_ACKNOWLEDGED:
        return (
            "⌛ Discord says that click was already answered (probably a "
            "double-click). Nothing to do."
        )
    return ""


def _exception_chain(error):
    """`error` and everything it was raised from, outermost first."""
    seen = []
    while error is not None and error not in seen:
        seen.append(error)
        error = error.__cause__ or error.__context__
    return seen


def describe_connection_failure(error: Exception) -> str:
    """Why we couldn't reach Discord, in plain words (no trailing period).

    Library tracebacks bury the real reason several "direct cause" levels
    deep (aiohttp → asyncio → socket), so look through the whole chain.
    """
    chain = _exception_chain(error)

    def find(kind):
        return next((e for e in chain if isinstance(e, kind)), None)

    # ClientConnectorDNSError only exists in aiohttp >= 3.11.
    dns_error = getattr(aiohttp, "ClientConnectorDNSError", socket.gaierror)
    if find(socket.gaierror) or find(dns_error):
        return (
            "can't look up Discord's address (DNS lookup failed) — this "
            "machine's internet connection or DNS server is down"
        )
    os_errors = [e for e in chain if isinstance(e, OSError) and e.errno is not None]
    errnos = {e.errno for e in os_errors}
    if errnos & {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN}:
        return (
            "network is unreachable — this machine has no working internet "
            "connection (Wi-Fi/Ethernet down, or the router is restarting)"
        )
    if errnos & {errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED}:
        return "the connection was refused or reset on the way to Discord"
    closed = find(discord.ConnectionClosed)
    if closed is not None:
        return f"Discord closed the connection (close code {closed.code})"
    if find(discord.GatewayNotFound):
        return (
            "Discord's gateway is unavailable — probably a Discord outage "
            "(see https://discordstatus.com)"
        )
    http = find(discord.HTTPException)
    if http is not None and http.status >= 500:
        return (
            f"Discord's servers answered with an error (HTTP {http.status}) — "
            "probably a Discord outage (see https://discordstatus.com)"
        )
    if find(TimeoutError) or find(aiohttp.ServerTimeoutError):
        return (
            "Discord didn't answer within 60 seconds — the connection is very "
            "slow or dropping packets"
        )
    if find(aiohttp.ServerDisconnectedError):
        return "Discord's server hung up on us"
    return f"{type(error).__name__}: {error}" if str(error) else type(error).__name__


def _fmt_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds}s" if seconds else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


class ReconnectLogFilter(logging.Filter):
    """Rewrite discord.py's reconnect tracebacks into one readable line each.

    While the network is down, discord.py logs ``Attempting a reconnect in
    Ns`` at ERROR with a ~60-line traceback on every retry. That's just the
    library doing its job, so this turns each one into a single WARNING that
    says *why* (DNS down, network unreachable, timeout …), counts the
    attempts, and lets :meth:`recovered` report how long the outage lasted.
    Attached to the ``discord.client`` logger; any other record passes as is.
    """

    _RECONNECT_MSG = "Attempting a reconnect in %.2fs"

    def __init__(self):
        super().__init__()
        self.outage_started = None  # time.monotonic() of the first failure
        self.attempts = 0

    def filter(self, record):
        if record.msg != self._RECONNECT_MSG or not record.exc_info:
            return True
        error = record.exc_info[1]
        retry = record.args[0] if record.args else 0
        reason = describe_connection_failure(error)
        now = time.monotonic()
        self.attempts += 1
        if self.outage_started is None:
            self.outage_started = now
            text = (
                f"🌐 Lost connection to Discord: {reason}. Retrying in "
                f"{retry:.0f}s — I reconnect on my own once the network is back."
            )
        else:
            text = (
                f"🌐 Still can't reach Discord (attempt {self.attempts}, offline "
                f"for {_fmt_duration(now - self.outage_started)}): {reason}. "
                f"Next try in {retry:.0f}s."
            )
        record.msg, record.args = text, ()
        record.exc_info = record.exc_text = None
        record.levelno, record.levelname = logging.WARNING, "WARNING"
        return True

    def recovered(self):
        """Call once connected again: a summary of the outage, or '' if none."""
        if self.outage_started is None:
            return ""
        took = _fmt_duration(time.monotonic() - self.outage_started)
        attempts = self.attempts
        self.outage_started, self.attempts = None, 0
        return (
            f"✅ Back online — Discord was unreachable for {took} "
            f"({attempts} failed attempt{'s' if attempts != 1 else ''})."
        )


reconnect_log = ReconnectLogFilter()
logging.getLogger("discord.client").addFilter(reconnect_log)


def log_handler_error(where, error):
    """Log an exception that escaped a button or command handler.

    Known, harmless ones (an expired click, a dropped connection, a missing
    permission) get one readable line. Anything else is a real bug and keeps
    its traceback — as does a generic Discord rejection, which usually means
    we sent something malformed.
    """
    friendly = describe_error(error)
    if friendly and type(error) is not discord.HTTPException:
        log.warning("While %s: %s", where, friendly)
    else:
        log.error("Unexpected error while %s:", where, exc_info=error)


# --------------------------------------------------------------------------- #
# Where to send notifications
# --------------------------------------------------------------------------- #
def _remember_channel(channel_id, guild_id):
    """Persist a runtime-learned notify channel (and its server) into the
    archive file."""
    archive_mod.set_keys(
        config.ARCHIVE_FILE, discord_channel_id=channel_id, discord_guild_id=guild_id
    )


class DiscordNotifier:
    """checker.Notifier for Discord: how each kind of message is phrased here."""

    def __init__(self, client):
        self.client = client

    @property
    def chat_known(self):
        return self.client.notify_channel_id is not None

    async def _channel(self):
        """The notify channel, from the cache or fetched over REST."""
        cid = int(self.client.notify_channel_id)
        return self.client.get_channel(cid) or await self.client.fetch_channel(cid)

    async def _send(self, text, **kwargs):
        channel = await self._channel()
        return await channel.send(text, **kwargs)

    async def send_video(self, v):
        await self._send(build_video_text(v), view=_video_view(v["video_id"]))

    async def send_first_run(self, watched):
        await self._send(
            "✅ **YtWatchlistBot is set up!**\n"
            f"Watching **{watched}** channel(s). I'll post here whenever a new "
            "video or livestream drops."
        )

    async def send_seeded(self, names):
        listing = checker.seeded_listing(names, discord.utils.escape_markdown)
        await self._send(f"👀 Now watching {len(names)} new channel(s):\n{listing}")

    async def send_auth_alert(self, reason):
        await self._send(
            "🔑 **YouTube needs re-authorizing**\n"
            f"{discord.utils.escape_markdown(reason)}\n\n"
            "I can't read your subscriptions, so checks are paused. Run "
            "/authorize and I'll walk you through reconnecting right here."
        )

    def describe_error(self, exc):
        return describe_error(exc)


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #
class WatchlistClient(discord.Client):
    def __init__(self):
        # Default (non-privileged) intents are plenty: slash commands and button
        # clicks arrive as interactions, never as message content.
        # No mentions: video and channel titles come from strangers, and an
        # "@everyone" in one would otherwise ping the whole server.
        super().__init__(
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.tree = app_commands.CommandTree(self)
        self.notify_channel_id = None
        self.notify_guild_id = None  # the server the notify channel is in
        self.check_state = {}
        self._check_task = None
        self._auth_task = None  # the running /authorize wait, if any

    # -- lifecycle -------------------------------------------------------- #
    async def setup_hook(self):
        # Resolve where to notify: configured value wins, else whatever /start
        # last registered (persisted in the archive).
        data = archive_mod.load(config.ARCHIVE_FILE)
        self.notify_channel_id = config.DISCORD_CHANNEL_ID or data.get("discord_channel_id")
        self.notify_guild_id = data.get("discord_guild_id")

        # A global sync can take up to an hour to show up in clients; syncing to
        # one guild is instant, so DISCORD_GUILD_ID is worth setting.
        if config.DISCORD_GUILD_ID:
            guild = discord.Object(id=int(config.DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            # Also publish an empty global set: commands registered globally
            # by an earlier run without DISCORD_GUILD_ID would otherwise stay,
            # and every command would be listed twice in this server.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info("Connected to Discord as %s.", self.user)
        self._log_recovery()
        if self.notify_channel_id is None:
            log.warning(
                "No notification channel yet — run /start in the channel you "
                "want me to post in."
            )
        if not youtube_playlist.is_configured():
            log.warning(
                "YouTube isn't connected yet — 💾 Save won't work until you run "
                "`python manual-setup/authorize_youtube.py` (see the README)."
            )
        # on_ready fires again after a reconnect; the loop must start only once.
        if self._check_task is None:
            self._check_task = self.loop.create_task(
                checker.check_loop(DiscordNotifier(self), self.check_state)
            )

    async def on_resumed(self):
        # A reconnect that picks the old session back up fires this, not on_ready.
        self._log_recovery()

    @staticmethod
    def _log_recovery():
        summary = reconnect_log.recovered()
        if summary:
            log.info(summary)

    async def on_error(self, event_method, /, *args, **kwargs):
        """An exception escaped an event handler (e.g. a button click)."""
        where = "handling a button click" if event_method == "on_interaction" else (
            f"handling {event_method}"
        )
        log_handler_error(where, sys.exc_info()[1])

    # -- authorization ---------------------------------------------------- #
    def authorized(self, channel_id):
        """Serve only the notify channel. If none is known yet, allow it so the
        owner's first /start can register one. Every command and button goes
        through this (or :meth:`may_register`), so a member of some other
        server the bot is in can neither use it nor move it. Within the
        channel, :meth:`may_control` limits who can change things."""
        target = self.notify_channel_id
        return target is None or str(channel_id) == str(target)

    def may_register(self, interaction):
        """May this /start (re)register its channel as the notify channel?

        Yes if no channel is known yet, or it's the current one; otherwise only
        someone who can manage the server the current channel is in — so the
        owner can still move the bot to another channel there (e.g. after
        deleting the old one), while an admin of some *other* server the bot
        was added to can't take it over.
        """
        if self.authorized(interaction.channel_id):
            return True
        perms = getattr(interaction.user, "guild_permissions", None)
        if not (perms and (perms.manage_guild or perms.administrator)):
            return False
        home = self._notify_guild_id()
        return home is not None and str(interaction.guild_id) == str(home)

    @staticmethod
    def may_control(interaction):
        """May this user change what the bot does (/authorize, /except, the
        buttons)? The notify channel can be shared with other server members,
        so the channel alone isn't enough: it takes Manage Server, as moving
        the channel with /start does. The owner has it, since adding a bot to
        a server needs it. A DM with the bot is the owner's own."""
        if interaction.guild_id is None:
            return True
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and (perms.manage_guild or perms.administrator))

    def _notify_guild_id(self):
        """The server the notify channel belongs to: from the live channel if
        it still exists, else as recorded by /start, else DISCORD_GUILD_ID."""
        try:
            channel = self.get_channel(int(self.notify_channel_id))
        except (TypeError, ValueError):
            channel = None
        guild = getattr(channel, "guild", None)
        if guild is not None:
            return guild.id
        return self.notify_guild_id or config.DISCORD_GUILD_ID or None

    async def run_check(self, *, trigger="scheduled"):
        await checker.run_check(
            DiscordNotifier(self), self.check_state, trigger=trigger
        )

    # -- button clicks ---------------------------------------------------- #
    async def on_interaction(self, interaction: discord.Interaction):
        """Save / Delete taps. Slash commands are dispatched by the tree."""
        if interaction.type is not discord.InteractionType.component:
            return
        if not self.authorized(interaction.channel_id):
            await interaction.response.send_message(
                "I only work in my notification channel.", ephemeral=True
            )
            return

        custom_id = (interaction.data or {}).get("custom_id", "")
        if custom_id != CB_SAVED and not self.may_control(interaction):
            await _not_allowed(interaction)
            return
        if custom_id.startswith(CB_SAVE):
            await self._on_save(interaction, custom_id[len(CB_SAVE):])
        elif custom_id.startswith(CB_DELETE):
            await self._on_delete(interaction)
        elif custom_id == CB_AUTH_YES:
            if self.auth_in_progress():
                await interaction.response.edit_message(
                    content="⏳ I'm already waiting for you to approve a code — "
                    "finish that one, or press ✖️ Cancel on it first.",
                    view=None,
                )
                return
            await self.begin_authorization(interaction, replace=True)
        elif custom_id == CB_AUTH_NO:
            await interaction.response.edit_message(
                content="👍 Kept the current YouTube connection.", view=None
            )
        elif custom_id == CB_AUTH_CANCEL:
            if self._auth_task is not None:
                self._auth_task.cancel()
                self._auth_task = None
            await interaction.response.edit_message(
                content="✖️ Cancelled — nothing was changed.", view=None
            )
        else:  # CB_SAVED or anything unrecognised
            await interaction.response.defer()

    async def _on_save(self, interaction, video_id):
        # Acknowledge first: add_video makes several Google API calls (token
        # refresh, playlist lookup/creation, insert) and Discord drops any
        # interaction not answered within 3 seconds — the video would still be
        # saved, but the click would show as failed and the button wouldn't
        # flip to ✅ Saved.
        await interaction.response.defer()
        try:
            result = await asyncio.to_thread(youtube_playlist.add_video, video_id)
        except youtube_playlist.NotAuthorized as exc:
            log.warning("Save blocked — YouTube not connected: %s", exc)
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001 - never fail a Save silently
            log.warning("Failed to add %s to YouTube: %s", video_id, exc)
            await interaction.followup.send(
                "⚠️ Couldn't add it to YouTube — please try again", ephemeral=True
            )
            return

        # Reflect the saved state, keeping Delete available.
        try:
            await interaction.message.edit(view=_video_view(video_id, saved=True))
        except discord.HTTPException as exc:
            log.warning("Couldn't update the Save button: %s", describe_error(exc) or exc)
        await interaction.followup.send(
            f"💾 Added to your '{config.YOUTUBE_PLAYLIST_NAME}' playlist"
            if result == "added"
            else "Already in your YouTube playlist",
            ephemeral=True,
        )
        log.info("Saved %s to the YouTube playlist (%s).", video_id, result)

    async def _on_delete(self, interaction):
        # Acknowledge first: deleting the message the click came from leaves
        # nothing to respond to, and an unanswered interaction shows as failed.
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except discord.HTTPException as exc:
            log.warning("Couldn't delete a message: %s", describe_error(exc) or exc)
            await interaction.followup.send(
                "Couldn't delete that message — see the log for why.",
                ephemeral=True,
            )


    # -- /authorize ------------------------------------------------------- #
    def auth_in_progress(self):
        return self._auth_task is not None and not self._auth_task.done()

    async def begin_authorization(self, interaction, *, replace=False):
        """Start Google's device flow and show the code (ephemerally).

        `replace=True` when called from the Yes button: the confirmation prompt
        is edited in place instead of sending a new ephemeral message. The
        click is acknowledged first, because Discord drops an interaction not
        answered within 3 seconds and asking Google for a code can take longer.
        """
        if replace:
            await interaction.response.defer()

        async def say(content, view=None):
            if replace:
                await interaction.edit_original_response(content=content, view=view)
                return interaction.edit_original_response
            msg = await interaction.followup.send(content, view=view, ephemeral=True, wait=True)
            return msg.edit

        # Discord dispatches interactions concurrently, so this check and the
        # claim below must happen with no await in between: a placeholder marks
        # the flow as pending while Google is still being asked for a code.
        if self.auth_in_progress():
            await say(
                "⏳ I'm already waiting for you to approve a code — finish that "
                "one, or press ✖️ Cancel on it first."
            )
            return
        placeholder = self.loop.create_future()
        self._auth_task = placeholder
        try:
            try:
                flow = await asyncio.to_thread(youtube_auth.start)
            except youtube_auth.DeviceAuthError as exc:  # includes ClientRejected
                await say(f"❌ Can't start authorization: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 - never leave the user without an answer
                log.error("Starting device authorization failed:", exc_info=exc)
                await say("😵 Something went wrong starting the authorization — see the log.")
                return

            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(
                label="✖️ Cancel", style=discord.ButtonStyle.secondary, custom_id=CB_AUTH_CANCEL,
            ))
            minutes = int(flow.seconds_left // 60)
            edit = await say(
                "🔑 **Connect YouTube**\n\n"
                f"1. On any device, open: {flow.verification_url}\n"
                f"2. Enter this code: **`{flow.user_code}`**\n\n"
                f"{_AUTH_TIP}\n\n"
                f"⏳ Waiting for you to approve… (the code is valid for about {minutes} min)",
                view=view,
            )
            self._auth_task = self.loop.create_task(self._wait_for_approval(flow, edit))
        finally:
            # Any early exit (or a failed send) releases the claim.
            if self._auth_task is placeholder:
                self._auth_task = None
            placeholder.cancel()

    async def _wait_for_approval(self, flow, edit):
        """Poll Google until you approve, then save the token and run a check.

        `edit` updates the ephemeral code message. Discord only lets us edit it
        for 15 minutes after the interaction, while the code can live ~30, so
        that edit is best-effort; the confirmation is also posted in the
        notify channel, once /start has registered one.
        """
        async def finish(content):
            try:
                await edit(content=content, view=None)
            except discord.HTTPException as exc:
                log.debug("Couldn't update the /authorize message: %s", exc)

        try:
            while True:
                await asyncio.sleep(flow.interval)
                creds = await asyncio.to_thread(youtube_auth.poll, flow)
                if creds is not None:
                    break
        except asyncio.CancelledError:
            return  # the Cancel button already answered
        except youtube_auth.DeviceAuthError as exc:
            await finish(f"❌ {exc}\nRun /authorize to try again.")
            return
        finally:
            self._auth_task = None

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
                f"**{discord.utils.escape_markdown(channel['title'])}**" if channel
                else "an account with no YouTube channel"
            )
        except Exception as exc:  # noqa: BLE001 - confirmation only
            log.warning("Couldn't read the connected channel: %s", exc)
            who = "your account"
        log.info("YouTube connected via /authorize (%s).", who)
        await finish(f"✅ Approved — connected as {who}.")
        # /authorize also works before /start; with no notify channel yet
        # there's nowhere to post, and the check below just seeds.
        if self.notify_channel_id is not None:
            try:
                await DiscordNotifier(self)._send(
                    f"✅ **YouTube connected** as {who}.\n"
                    "Your subscriptions are now the watchlist and 💾 Save goes to "
                    f"'{config.YOUTUBE_PLAYLIST_NAME}' there. Checking now…"
                )
            except discord.HTTPException as exc:
                log.warning("Couldn't post the connection notice: %s", describe_error(exc) or exc)
        await self.run_check(trigger="manual")


# --------------------------------------------------------------------------- #
# Slash commands
# --------------------------------------------------------------------------- #
def _wrong_channel(interaction):
    return interaction.response.send_message(
        "I only work in my notification channel.", ephemeral=True
    )


def _not_allowed(interaction):
    return interaction.response.send_message(
        "Only someone with the Manage Server permission can do that.", ephemeral=True
    )


def _register_commands(client: WatchlistClient):
    tree = client.tree

    @tree.error
    async def on_command_error(interaction: discord.Interaction, error):
        command = getattr(interaction.command, "name", None)
        where = f"running /{command}" if command else "running a command"
        log_handler_error(where, getattr(error, "original", error))

    @tree.command(name="start", description="Post notifications in this channel")
    async def start(interaction: discord.Interaction):
        if not client.may_register(interaction):
            await interaction.response.send_message(
                "I'm already posting in another channel. Someone with the "
                "Manage Server permission in that channel's server can run "
                "/start in another channel there to move me.",
                ephemeral=True,
            )
            return
        pinned = config.DISCORD_CHANNEL_ID
        if pinned and str(interaction.channel_id) != str(pinned):
            await interaction.response.send_message(
                "ℹ️ My channel is pinned by DISCORD_CHANNEL_ID in conf.env, so "
                "/start can't move it. Change it there and restart me.",
                ephemeral=True,
            )
            return
        # In the pinned channel itself there's nothing to register, only the
        # how-to below (as on Telegram with TELEGRAM_CHAT_ID set).
        if not pinned:
            client.notify_channel_id = str(interaction.channel_id)
            guild_id = str(interaction.guild_id) if interaction.guild_id else None
            client.notify_guild_id = guild_id
            await asyncio.to_thread(_remember_channel, client.notify_channel_id, guild_id)
            log.info("Notifications will go to channel %s.", client.notify_channel_id)
        await interaction.response.send_message(
            "👋 I'll post here when a watched channel puts out a new video or "
            "livestream.\n\n"
            "On each message: 💾 Save adds it to your "
            f"'{config.YOUTUBE_PLAYLIST_NAME}' playlist on YouTube, 🗑 Delete "
            "removes the message.\n\n"
            "Commands:\n"
            "• /check — check the channels right now\n"
            "• /except — mute a channel or limit it to videos / livestreams\n"
            "• /authorize — connect (or reconnect) your YouTube account\n"
            "• /help — how it works"
        )

    @tree.command(name="help", description="How this bot works")
    async def help_command(interaction: discord.Interaction):
        if not client.authorized(interaction.channel_id):
            await _wrong_channel(interaction)
            return
        await interaction.response.send_message(
            "**How I work**\n\n"
            f"Every {config.CHECK_INTERVAL_MINUTES} minute(s) I read the RSS feed "
            "of each channel you're **subscribed to on YouTube** and post about "
            "any new video or livestream (Shorts are skipped). Opt out of "
            "channels or types with /except or in `exceptions.txt`.\n\n"
            "• 💾 **Save** adds the video to your "
            f"'{config.YOUTUBE_PLAYLIST_NAME}' playlist on YouTube (idempotent).\n"
            "• 🗑 **Delete** removes that message.\n"
            "• /check runs a check immediately instead of waiting.\n"
            "• /except adds an opt-out for one channel.\n"
            "• /authorize connects your YouTube account from here (no server "
            "access needed): I show a code, you enter it on google.com/device.",
            ephemeral=True,
        )

    @tree.command(name="check", description="Check your channels right now")
    async def check_command(interaction: discord.Interaction):
        if not client.authorized(interaction.channel_id):
            await _wrong_channel(interaction)
            return
        if checker.check_lock().locked():
            await interaction.response.send_message(
                "⏳ A check is already running…", ephemeral=True
            )
            return
        # A full pass easily outruns Discord's 3-second reply window.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await client.run_check(trigger="manual")
        except Exception as exc:  # noqa: BLE001 - the deferred reply must be answered
            friendly = describe_error(exc)
            if friendly:
                log.warning("During /check: %s", friendly)
            else:
                log.error("Unexpected error during /check:", exc_info=exc)
            await interaction.followup.send(
                "😵 The check failed — see the log.", ephemeral=True
            )
            return
        await interaction.followup.send("🔎 Check done.", ephemeral=True)

    @tree.command(name="authorize", description="Connect (or reconnect) your YouTube account")
    async def authorize_command(interaction: discord.Interaction):
        if not client.authorized(interaction.channel_id):
            await _wrong_channel(interaction)
            return
        if not client.may_control(interaction):
            await _not_allowed(interaction)
            return
        if client.auth_in_progress():
            await interaction.response.send_message(
                "⏳ I'm already waiting for you to approve a code — finish that "
                "one, or press ✖️ Cancel on it first.",
                ephemeral=True,
            )
            return
        # Reading the current channel / starting the flow takes a few round
        # trips; only the invoker should see the code, hence ephemeral.
        await interaction.response.defer(ephemeral=True, thinking=True)

        if youtube_playlist.is_configured():
            try:
                channel = await asyncio.to_thread(youtube_playlist.acting_channel)
                current = (
                    "YouTube is currently connected as "
                    f"**{discord.utils.escape_markdown(channel['title'])}**."
                    if channel else "YouTube is connected, but that account has no channel."
                )
            except youtube_playlist.NotAuthorized as exc:
                current = f"The current YouTube connection is broken: {exc}"
            except Exception as exc:  # noqa: BLE001 - the name is a nicety only
                log.warning("Couldn't read the connected channel: %s", exc)
                current = "YouTube is currently connected (couldn't read which channel)."
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(
                label="🔑 Yes, reconnect", style=discord.ButtonStyle.primary, custom_id=CB_AUTH_YES,
            ))
            view.add_item(discord.ui.Button(
                label="✖️ Keep it", style=discord.ButtonStyle.secondary, custom_id=CB_AUTH_NO,
            ))
            await interaction.followup.send(
                f"{current}\nReconnect anyway? The new account's subscriptions become "
                "the watchlist and its playlist gets the saves.",
                view=view, ephemeral=True,
            )
            return

        await client.begin_authorization(interaction)

    @tree.command(
        name="except", description="Mute a channel or limit what it notifies about"
    )
    @app_commands.describe(
        channel="Channel name, @handle, URL or UC… id (from your subscriptions)",
        rule="What to opt out of",
    )
    @app_commands.choices(
        rule=[
            app_commands.Choice(name="🔕 Mute the channel", value="mute"),
            app_commands.Choice(name="▶️ Only videos", value="videos"),
            app_commands.Choice(name="📺 Only livestreams", value="livestreams"),
        ]
    )
    async def except_command(
        interaction: discord.Interaction,
        channel: str,
        rule: app_commands.Choice[str],
    ):
        if not client.authorized(interaction.channel_id):
            await _wrong_channel(interaction)
            return
        if not client.may_control(interaction):
            await _not_allowed(interaction)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            matches = await asyncio.to_thread(discovery.lookup_subscription, channel)
        except youtube_playlist.NotAuthorized as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001 - surface a friendly error, don't crash
            log.warning("Subscription lookup failed: %s", exc)
            await interaction.followup.send(
                "😵 Couldn't read your subscriptions right now — try again in a "
                "moment.",
                ephemeral=True,
            )
            return

        esc = discord.utils.escape_markdown
        if not matches:
            await interaction.followup.send(
                f"❌ I couldn't find “{esc(channel)}” in your subscriptions. "
                "Check the spelling, or make sure you're subscribed to it.",
                ephemeral=True,
            )
            return
        if len(matches) > 1:
            listing = "\n".join(f"• {esc(m['title'])}" for m in matches[:10])
            more = "\n…" if len(matches) > 10 else ""
            await interaction.followup.send(
                f"🔎 That matches more than one subscription:\n{listing}{more}\n\n"
                "Try again with a more specific name or the @handle.",
                ephemeral=True,
            )
            return

        target = matches[0]
        chosen = _EXC_RULES[rule.value]
        try:
            outcome = await asyncio.to_thread(
                discovery.set_exception,
                config.EXCEPTIONS_FILE, target["id"], chosen, target["title"],
            )
        except Exception as exc:  # noqa: BLE001 - never fail silently
            log.warning("Couldn't write exception for %s: %s", target["id"], exc)
            await interaction.followup.send(
                "😵 Couldn't save that exception — please try again.", ephemeral=True
            )
            return

        verb = "Updated" if outcome == "updated" else "Added"
        await interaction.followup.send(
            f"✅ {verb}: **{esc(target['title'])}** → {_EXC_DESC[chosen]}.\n"
            "Takes effect on the next check.",
            ephemeral=True,
        )
        log.info(
            "Exception %s: %s (%s) -> %s", outcome, target["title"], target["id"], chosen
        )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _is_discord_id(value):
    """True for a Discord snowflake id: ASCII digits only."""
    return value.isascii() and value.isdigit()


def main():
    if not config.DISCORD_BOT_TOKEN:
        print(
            "ERROR: No Discord bot token found.\n\n"
            "Create an application at "
            "https://discord.com/developers/applications, add a Bot and hit "
            "Reset Token, then either:\n"
            "  • put DISCORD_BOT_TOKEN=... in a conf.env file next to this script,\n"
            "  • set a DISCORD_BOT_TOKEN environment variable, or\n"
            "  • create a discord_token.txt file next to this script with the token.\n"
        )
        return

    # Discord ids are plain numbers. Anything else (a pasted channel link, a
    # "#name") would make every notification fail and every command answer
    # "wrong channel", or crash the command registration at startup.
    bad = [
        name for name in ("DISCORD_CHANNEL_ID", "DISCORD_GUILD_ID")
        if getattr(config, name) and not _is_discord_id(getattr(config, name))
    ]
    if bad:
        print(
            f"ERROR: {' and '.join(bad)} must be a numeric Discord id, e.g. "
            "123456789012345678.\n\n"
            "Turn on Developer Mode (User Settings -> Advanced), then right-click "
            "the channel or server -> Copy ID. Or remove the line from conf.env "
            "and use /start instead.\n"
        )
        return

    client = WatchlistClient()
    _register_commands(client)
    log.info(
        "YtWatchlistBot is running on Discord (checking every %s min). "
        "Ctrl+C to stop.",
        config.CHECK_INTERVAL_MINUTES,
    )
    try:
        # log_handler=None keeps our logging setup instead of discord.py's.
        client.run(config.DISCORD_BOT_TOKEN, log_handler=None)
    except discord.LoginFailure as exc:
        log.error(describe_error(exc))


if __name__ == "__main__":
    main()
