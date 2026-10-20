"""/start gating and button handling for both chat backends. Each test needs
only its own chat library, so a single-backend install still runs that
backend's tests (the setup_bot tests live in test_setup_bot.py)."""
import asyncio

import pytest

try:
    import telegram
    import ytwatchlistbot as tgbot
except ImportError:
    telegram = tgbot = None
try:
    import discordbot
except ImportError:
    discordbot = None
needs_telegram = pytest.mark.skipif(tgbot is None, reason="python-telegram-bot not installed")
needs_discord = pytest.mark.skipif(discordbot is None, reason="discord.py not installed")


# --- Telegram /start gating ------------------------------------------------- #
class _App:
    def __init__(self, target):
        self.bot_data = {"notify_chat_id": target}


class _Ctx:
    def __init__(self, target):
        self.application = _App(target)
        self.bot_data = self.application.bot_data


class _Msg:
    def __init__(self):
        self.replies = []
        self.kwargs = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        self.kwargs.append(kw)


class _Update:
    def __init__(self, chat_id, text=None):
        self.effective_chat = type("Chat", (), {"id": chat_id})()
        self.message = _Msg()
        self.message.text = text


@needs_telegram
def test_telegram_first_start_registers(monkeypatch):
    persisted = []
    monkeypatch.setattr(tgbot, "_remember_chat", persisted.append)
    monkeypatch.setattr(tgbot.config, "TELEGRAM_CHAT_ID", "")
    ctx, upd = _Ctx(None), _Update(111)
    asyncio.run(tgbot.start(upd, ctx))
    assert ctx.bot_data["notify_chat_id"] == "111" and persisted == ["111"]
    assert tgbot._authorized(ctx, 111)


@needs_telegram
def test_telegram_start_from_another_chat_is_refused(monkeypatch):
    """Regression: any /start used to re-point notifications and steal
    authorization from the owner."""
    persisted = []
    monkeypatch.setattr(tgbot, "_remember_chat", persisted.append)
    monkeypatch.setattr(tgbot.config, "TELEGRAM_CHAT_ID", "")
    ctx, upd = _Ctx("111"), _Update(999)
    asyncio.run(tgbot.start(upd, ctx))
    assert ctx.bot_data["notify_chat_id"] == "111" and persisted == []
    assert upd.message.replies == [tgbot._NOT_YOUR_CHAT]
    assert tgbot._authorized(ctx, 111) and not tgbot._authorized(ctx, 999)


@needs_telegram
def test_telegram_help_ignores_strangers():
    ctx, upd = _Ctx("111"), _Update(999)
    asyncio.run(tgbot.help_command(upd, ctx))
    assert upd.message.replies == []


@needs_telegram
def test_telegram_help_escapes_the_playlist_name(monkeypatch):
    """Regression (F-062): /help is sent as HTML with the playlist name
    unescaped, so a name with "<" made Telegram reject it and /help went
    unanswered."""
    monkeypatch.setattr(tgbot.config, "YOUTUBE_PLAYLIST_NAME", "Saves <3 & co")
    ctx, upd = _Ctx("111"), _Update(111)
    asyncio.run(tgbot.help_command(upd, ctx))
    assert upd.message.kwargs[0].get("parse_mode") == tgbot.ParseMode.HTML
    assert "'Saves &lt;3 &amp; co'" in upd.message.replies[0]


@needs_telegram
def test_telegram_except_no_match_reply_is_html(monkeypatch):
    """Regression (F-009): the escaped channel name was sent as plain text, so
    `A&B` showed up as `A&amp;B`."""
    monkeypatch.setattr(tgbot.discovery, "lookup_subscription", lambda text: [])
    ctx, upd = _Ctx("111"), _Update(111, text="A&B")
    asyncio.run(tgbot.except_channel(upd, ctx))
    assert "A&amp;B" in upd.message.replies[0]
    assert upd.message.kwargs[0].get("parse_mode") == tgbot.ParseMode.HTML


@needs_telegram
def test_telegram_yes_does_not_start_a_second_flow(monkeypatch):
    """Regression (F-011): Yes on a second prompt started another device flow
    while one was already waiting."""
    started = []

    async def fake_begin(app, chat_id, edit=None):
        started.append(chat_id)

    monkeypatch.setattr(tgbot, "_begin_authorization", fake_begin)

    class _Query:
        data = f"{tgbot.CB_AUTH}yes"

        def __init__(self):
            self.edits = []

        async def answer(self, *a, **k):
            pass

        async def edit_message_text(self, text, **kw):
            self.edits.append(text)

    async def scenario():
        ctx = _Ctx("111")
        pending = asyncio.get_running_loop().create_future()  # not done
        ctx.bot_data["auth_task"] = pending
        q = _Query()
        upd = type("U", (), {"callback_query": q, "effective_chat": type("C", (), {"id": 111})()})()
        await tgbot.on_auth_button(upd, ctx)
        pending.cancel()
        return q

    q = asyncio.run(scenario())
    assert started == []
    assert "already waiting" in q.edits[0]


# --- Discord /start gating -------------------------------------------------- #
class _Perms:
    def __init__(self, manage_guild=False, administrator=False):
        self.manage_guild = manage_guild
        self.administrator = administrator


class _Interaction:
    def __init__(self, channel_id, perms=None, guild_id=None):
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.user = type("User", (), {"guild_permissions": perms})()


HOME_GUILD = 100


def _client(target, *, channel_guild=HOME_GUILD, stored_guild=None):
    """A client whose notify channel `target` lives in `channel_guild` (None =
    the channel no longer exists)."""
    client = discordbot.WatchlistClient.__new__(discordbot.WatchlistClient)
    client.notify_channel_id = target
    client.notify_guild_id = stored_guild
    guild = type("G", (), {"id": channel_guild})() if channel_guild else None
    channel = type("Ch", (), {"guild": guild})() if guild else None
    client.get_channel = lambda cid: channel
    return client


@needs_discord
def test_discord_may_register_rules(monkeypatch):
    monkeypatch.setattr(discordbot.config, "DISCORD_GUILD_ID", "")
    manager = _Perms(manage_guild=True)
    assert _client(None).may_register(_Interaction(5))
    assert _client("5").may_register(_Interaction(5))
    assert not _client("5").may_register(_Interaction(9, _Perms(), HOME_GUILD))
    assert not _client("5").may_register(_Interaction(9, None))  # DM
    # A manager of the notify channel's own server can move it there...
    assert _client("5").may_register(_Interaction(9, manager, HOME_GUILD))
    assert _client("5").may_register(_Interaction(9, _Perms(administrator=True), HOME_GUILD))


@needs_discord
def test_discord_admin_of_another_server_cannot_take_over(monkeypatch):
    """Regression (F-005): Manage Server in *any* server the bot had been added
    to was enough to move the notify channel there."""
    monkeypatch.setattr(discordbot.config, "DISCORD_GUILD_ID", "")
    outsider = _Interaction(9, _Perms(manage_guild=True, administrator=True), 777)
    assert not _client("5").may_register(outsider)
    # Old channel deleted: the server recorded by /start still decides.
    gone = _client("5", channel_guild=None, stored_guild=str(HOME_GUILD))
    assert not gone.may_register(outsider)
    assert gone.may_register(_Interaction(9, _Perms(manage_guild=True), HOME_GUILD))
    # Nothing recorded (legacy archive) and no DISCORD_GUILD_ID: nobody moves it.
    assert not _client("5", channel_guild=None).may_register(outsider)


@needs_telegram
def test_telegram_except_restarts_a_pending_conversation():
    """Regression (F-027): after /except showed its buttons and they weren't
    tapped, every later /except matched no handler and got no reply."""
    bot = telegram.Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot._bot_user = telegram.User(id=1, first_name="b", is_bot=True, username="mybot")
    update = telegram.Update.de_json({
        "update_id": 1,
        "message": {
            "message_id": 1, "date": 0, "text": "/except",
            "chat": {"id": 111, "type": "private"},
            "from": {"id": 111, "is_bot": False, "first_name": "x"},
            "entities": [{"type": "bot_command", "offset": 0, "length": 7}],
        },
    }, bot)
    conv = tgbot.except_conversation()
    conv._conversations[(111, 111)] = tgbot.EXC_TYPE  # buttons shown, never tapped
    assert conv.check_update(update) is not None


@needs_telegram
def test_telegram_check_runs_in_the_background(monkeypatch):
    """Regression (F-034): /check awaited the whole check in its handler, and
    python-telegram-bot handles one update at a time, so every tap and command
    went unanswered until the check finished."""
    release = []

    async def long_check(app, *, trigger):
        release.append(trigger)
        await asyncio.Event().wait()  # a check that never finishes

    monkeypatch.setattr(tgbot, "run_check", long_check)

    async def scenario():
        ctx = _Ctx("111")
        tasks = []
        ctx.application.create_task = lambda coro, update=None: tasks.append(
            asyncio.get_running_loop().create_task(coro)
        )
        upd = _Update(111)
        # Returns at once instead of blocking on the check.
        await asyncio.wait_for(tgbot.check_command(upd, ctx), timeout=1)
        await asyncio.sleep(0)
        assert release == ["manual"] and len(tasks) == 1
        assert upd.message.replies == ["🔎 Checking your channels now…"]
        tasks[0].cancel()

    asyncio.run(scenario())


class _Response:
    def __init__(self):
        self.sent = []

    async def send_message(self, text, **kw):
        self.sent.append(text)


@needs_discord
def test_discord_start_in_the_pinned_channel_shows_the_howto(monkeypatch):
    """Regression (F-035): with DISCORD_CHANNEL_ID set, /start in that very
    channel answered "can't move it" instead of the how-to."""
    monkeypatch.setattr(discordbot.config, "DISCORD_CHANNEL_ID", "5")
    remembered = []
    monkeypatch.setattr(discordbot, "_remember_channel", lambda *a: remembered.append(a))
    client = discordbot.WatchlistClient()
    client.notify_channel_id = "5"
    client.notify_guild_id = str(HOME_GUILD)  # so a manager there passes may_register
    discordbot._register_commands(client)
    start = client.tree.get_command("start").callback

    def interaction(channel_id, perms=None):
        i = _Interaction(channel_id, perms, HOME_GUILD)
        i.response = _Response()
        return i

    here = interaction(5)
    asyncio.run(start(here))
    assert "Commands:" in here.response.sent[0] and "pinned" not in here.response.sent[0]
    # Elsewhere, even a server manager is told the channel is pinned.
    elsewhere = interaction(9, _Perms(manage_guild=True))
    asyncio.run(start(elsewhere))
    assert "pinned" in elsewhere.response.sent[0]
    assert client.notify_channel_id == "5" and remembered == []


@needs_discord
def test_discord_describe_error_keeps_local_errors_local():
    """Regression (F-041): any OSError, e.g. an archive.json that couldn't be
    written, was reported as "Connection to Discord dropped" with no
    traceback."""
    import aiohttp

    assert discordbot.describe_error(FileNotFoundError(2, "No such file")) == ""
    assert discordbot.describe_error(PermissionError(13, "Permission denied")) == ""
    assert "Connection to Discord" in discordbot.describe_error(
        aiohttp.ServerDisconnectedError()
    )


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, text, **kw):
        self.sent.append(text)


@needs_discord
def test_discord_members_without_manage_server_cannot_control(monkeypatch):
    """Regression (F-042): anyone in the notify channel could reconnect
    YouTube to their own account, add opt-outs, or use the buttons."""
    monkeypatch.setattr(discordbot.config, "DISCORD_CHANNEL_ID", "")
    monkeypatch.setattr(discordbot.youtube_playlist, "is_configured", lambda: True)
    monkeypatch.setattr(discordbot.youtube_playlist, "acting_channel",
                        lambda: {"id": "UC1", "title": "Owner"})
    touched = []
    monkeypatch.setattr(discordbot.discovery, "lookup_subscription",
                        lambda text: touched.append("lookup") or [])
    monkeypatch.setattr(discordbot.youtube_playlist, "add_video",
                        lambda vid: touched.append("save") or "added")
    client = discordbot.WatchlistClient()
    client.notify_channel_id = "5"
    discordbot._register_commands(client)
    command = lambda name: client.tree.get_command(name).callback

    class _Resp(_Response):
        async def defer(self, **kw):
            pass

    def interaction(perms, custom_id=None):
        i = _Interaction(5, perms, HOME_GUILD)
        i.response, i.followup = _Resp(), _Followup()
        i.type = discordbot.discord.InteractionType.component
        i.data = {"custom_id": custom_id}
        i.message = None
        return i

    member = _Perms()
    rule = discordbot.app_commands.Choice(name="mute", value="mute")
    calls = [
        lambda i: command("authorize")(i),
        lambda i: command("except")(i, "Some Channel", rule),
        lambda i: client.on_interaction(i),
    ]
    for call, custom_id in zip(calls, [None, None, f"{discordbot.CB_SAVE}abc"]):
        i = interaction(member, custom_id)
        asyncio.run(call(i))
        assert "Manage Server" in i.response.sent[0] and i.followup.sent == []
    assert touched == []

    # The owner (Manage Server) still gets through.
    owner = interaction(_Perms(manage_guild=True))
    asyncio.run(command("authorize")(owner))
    assert "currently connected as **Owner**" in owner.followup.sent[0]


@needs_telegram
def test_telegram_shutdown_does_not_wait_for_a_pending_authorize(monkeypatch):
    """Regression (F-043): the /authorize wait was an Application.create_task
    task, which Application.stop() awaits, so stopping the bot with a code
    still pending hung until the code expired (~30 minutes)."""
    import time

    from telegram.ext import ApplicationBuilder

    class _StubBot(telegram.Bot):
        async def initialize(self):
            self._bot_user = telegram.User(id=1, first_name="b", is_bot=True, username="b")
            self._initialized = True

        async def shutdown(self):
            pass

        async def send_message(self, chat_id, text, **kw):
            return type("M", (), {"message_id": 1})()

    monkeypatch.setattr(tgbot.youtube_auth, "start", lambda secret_file=None: tgbot.youtube_auth.DeviceFlow(
        "CODE", "https://www.google.com/device", "dev", 0.05, time.monotonic() + 1800, "c", "s"))
    monkeypatch.setattr(tgbot.youtube_auth, "poll", lambda flow: None)  # never approved

    async def scenario():
        app = ApplicationBuilder().bot(_StubBot("123:abc")).updater(None).build()
        await app.initialize()
        await app.start()
        await tgbot._begin_authorization(app, 111)
        task = app.bot_data["auth_task"]
        await asyncio.wait_for(app.stop(), timeout=2)  # used to block here
        assert not task.done()
        task.cancel()
        await app.shutdown()

    asyncio.run(scenario())


@needs_discord
def test_discord_client_never_mentions():
    """Regression (F-053): the client used Discord's default mention parsing,
    so a video titled "@everyone ..." pinged the whole server."""
    mentions = discordbot.WatchlistClient().allowed_mentions
    assert mentions is not None
    assert not (mentions.everyone or mentions.users or mentions.roles or mentions.replied_user)


@needs_discord
def test_discord_check_failure_is_answered(monkeypatch):
    """Regression (F-054): /check deferred its reply, and a check that raised
    left it on "thinking…" for good."""
    monkeypatch.setattr(discordbot.config, "DISCORD_CHANNEL_ID", "")
    client = discordbot.WatchlistClient()
    client.notify_channel_id = "5"

    async def failing_check(*, trigger):
        raise FileNotFoundError(2, "No such file or directory")

    client.run_check = failing_check
    discordbot._register_commands(client)

    class _Resp(_Response):
        async def defer(self, **kw):
            pass

    i = _Interaction(5, None, HOME_GUILD)
    i.response, i.followup = _Resp(), _Followup()
    asyncio.run(client.tree.get_command("check").callback(i))
    assert i.followup.sent == ["😵 The check failed — see the log."]


@needs_discord
def test_discord_guild_sync_clears_global_commands(monkeypatch, archive_path):
    """Regression (F-055): with DISCORD_GUILD_ID set only the guild copy was
    synced, so commands registered globally by an earlier run stayed and
    every command was listed twice in that server."""
    monkeypatch.setattr(discordbot.config, "DISCORD_GUILD_ID", str(HOME_GUILD))
    monkeypatch.setattr(discordbot.config, "DISCORD_CHANNEL_ID", "")
    client = discordbot.WatchlistClient()
    discordbot._register_commands(client)
    synced = {}

    async def sync(*, guild=None):
        key = guild.id if guild else "global"
        synced[key] = sorted(c.name for c in client.tree.get_commands(guild=guild))

    client.tree.sync = sync
    asyncio.run(client.setup_hook())
    assert synced["global"] == []
    assert synced[HOME_GUILD] == ["authorize", "check", "except", "help", "start"]


@needs_discord
@pytest.mark.parametrize("name,value", [
    ("DISCORD_CHANNEL_ID", "https://discord.com/channels/100/5"),
    ("DISCORD_GUILD_ID", "#general"),
])
def test_discord_rejects_a_non_numeric_id(monkeypatch, capsys, name, value):
    """Regression (F-036): a non-numeric channel id made every send fail and
    every command say "wrong channel"; a non-numeric guild id crashed startup."""
    monkeypatch.setattr(discordbot.config, "DISCORD_BOT_TOKEN", "t")
    monkeypatch.setattr(discordbot.config, "DISCORD_CHANNEL_ID", "")
    monkeypatch.setattr(discordbot.config, "DISCORD_GUILD_ID", "")
    monkeypatch.setattr(discordbot.config, name, value)

    def no_client():
        raise AssertionError("the client must not start")

    monkeypatch.setattr(discordbot, "WatchlistClient", no_client)
    discordbot.main()
    assert f"{name} must be a numeric Discord id" in capsys.readouterr().out


# --- Discord error readability ---------------------------------------------- #
def _chained(outer, inner):
    """`outer` raised from `inner`, as aiohttp/discord.py build them."""
    try:
        try:
            raise inner
        except BaseException as exc:
            raise outer from exc
    except BaseException as exc:
        return exc


def _reconnect_record(error, retry=1.4):
    import logging

    return logging.LogRecord(
        "discord.client", logging.ERROR, __file__, 0,
        "Attempting a reconnect in %.2fs", (retry,), (type(error), error, None),
    )


@needs_discord
def test_discord_reconnect_traceback_becomes_one_line():
    """A network outage logged a ~60-line traceback per retry; now each retry
    is one WARNING saying why, and reconnecting reports how long it lasted."""
    import logging
    import socket

    import aiohttp

    dns = _chained(
        aiohttp.ClientConnectionError("Cannot connect to host gateway.discord.gg:443"),
        socket.gaierror(-3, "Temporary failure in name resolution"),
    )
    f = discordbot.ReconnectLogFilter()
    first = _reconnect_record(dns)
    assert f.filter(first)
    assert first.levelno == logging.WARNING and first.exc_info is None
    assert "Lost connection" in first.getMessage() and "DNS" in first.getMessage()

    second = _reconnect_record(dns, retry=28.7)
    f.filter(second)
    assert "attempt 2" in second.getMessage() and "29s" in second.getMessage()

    assert "2 failed attempts" in f.recovered()
    assert f.recovered() == ""  # reset once reported

    other = logging.LogRecord("discord.client", logging.ERROR, __file__, 0, "x %s", ("y",), None)
    assert f.filter(other) and other.getMessage() == "x y"


@needs_discord
def test_discord_connection_failure_reasons():
    import errno

    unreachable = _chained(
        ConnectionError("Cannot connect"), OSError(errno.ENETUNREACH, "Network is unreachable")
    )
    assert "network is unreachable" in discordbot.describe_connection_failure(unreachable)
    timeout = _chained(TimeoutError(), asyncio.CancelledError())
    assert "60 seconds" in discordbot.describe_connection_failure(timeout)


class _Resp:
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


@needs_discord
def test_discord_expired_interaction_is_one_readable_line(caplog):
    """A click answered after Discord's 3-second window raised 404 "Unknown
    interaction" (10062), logged as an opaque traceback."""
    expired = discordbot.discord.NotFound(
        _Resp(404, "Not Found"), {"code": 10062, "message": "Unknown interaction"}
    )
    assert "3 seconds" in discordbot.describe_error(expired)

    client = _client("5")
    try:
        raise expired
    except discordbot.discord.NotFound:
        asyncio.run(client.on_error("on_interaction"))
    record = caplog.records[-1]
    assert record.levelname == "WARNING" and record.exc_info is None
    assert "button click" in record.getMessage() and "try again" in record.getMessage()


@needs_discord
def test_discord_unexpected_handler_error_keeps_traceback(caplog):
    client = _client("5")
    try:
        raise KeyError("bug")
    except KeyError:
        asyncio.run(client.on_error("on_interaction"))
    record = caplog.records[-1]
    assert record.levelname == "ERROR" and record.exc_info is not None
