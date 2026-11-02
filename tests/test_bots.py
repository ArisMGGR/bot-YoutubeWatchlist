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
        self.effective_chat = type("Chat", (), {"id": chat_id, "type": "private"})()
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
        upd = type("U", (), {"callback_query": q, "effective_chat": type("C", (), {"id": 111, "type": "private"})()})()
        await tgbot.on_auth_button(upd, ctx)
        pending.cancel()
        return q

    q = asyncio.run(scenario())
    assert started == []
    assert "already waiting" in q.edits[0]


@needs_telegram
def test_telegram_strangers_reach_no_handler(monkeypatch):
    """Regression (F-076): only /start, /help and /authorize were tested
    against a stranger's chat, so the gate on the buttons, /check and /except
    could be dropped with every test still passing."""
    touched = []
    monkeypatch.setattr(tgbot.youtube_playlist, "add_video", lambda vid: touched.append("save"))
    monkeypatch.setattr(tgbot.youtube_playlist, "is_configured",
                        lambda: touched.append("except") or True)

    async def fake_begin(app, chat_id, edit=None):
        touched.append("authorize")

    async def fake_check(app, *, trigger):
        touched.append("check")

    monkeypatch.setattr(tgbot, "_begin_authorization", fake_begin)
    monkeypatch.setattr(tgbot, "run_check", fake_check)

    class _Query:
        def __init__(self, data):
            self.data = data
            self.answers, self.edits = [], []

        async def answer(self, *a, **k):
            self.answers.append(a)

        async def edit_message_text(self, text, **kw):
            self.edits.append(text)

        async def edit_message_reply_markup(self, *a, **k):
            touched.append("markup")

        async def delete_message(self):
            touched.append("delete")

    def tap(data):
        q = _Query(data)
        return q, type("U", (), {"callback_query": q, "effective_chat": type("C", (), {"id": 999, "type": "private"})()})()

    async def scenario():
        ctx = _Ctx("111")
        ctx.user_data = {}
        for data in (f"{tgbot.CB_SAVE}abc", f"{tgbot.CB_DELETE}abc"):
            q, upd = tap(data)
            await tgbot.on_button(upd, ctx)
            assert q.answers == [()]  # the spinner is closed, nothing else
        q, upd = tap(f"{tgbot.CB_AUTH}yes")
        await tgbot.on_auth_button(upd, ctx)
        assert q.edits == []
        stranger = _Update(999)
        await tgbot.check_command(stranger, ctx)
        await asyncio.sleep(0)  # a check started by mistake would run here
        assert "check_task" not in ctx.bot_data
        assert await tgbot.except_start(stranger, ctx) == tgbot.ConversationHandler.END
        assert stranger.message.replies == []

    asyncio.run(scenario())
    assert touched == []


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
    # ...but DISCORD_GUILD_ID names the home server (regression, F-099).
    monkeypatch.setattr(discordbot.config, "DISCORD_GUILD_ID", str(HOME_GUILD))
    legacy = _client("5", channel_guild=None)
    assert legacy.may_register(_Interaction(9, _Perms(manage_guild=True), HOME_GUILD))
    assert not legacy.may_register(outsider)


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
        upd = _Update(111)
        # Returns at once instead of blocking on the check.
        await asyncio.wait_for(tgbot.check_command(upd, ctx), timeout=1)
        await asyncio.sleep(0)
        assert release == ["manual"]
        assert upd.message.replies == ["🔎 Checking your channels now…"]
        ctx.bot_data["check_task"].cancel()

    asyncio.run(scenario())


@needs_telegram
def test_telegram_check_failure_is_answered(monkeypatch):
    """A /check that raises is logged and answered in the chat (it used to
    reach PTB's error handler through application.create_task)."""
    async def failing_check(app, *, trigger):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(tgbot, "run_check", failing_check)

    async def scenario():
        ctx, upd = _Ctx("111"), _Update(111)
        await tgbot.check_command(upd, ctx)
        await ctx.bot_data["check_task"]
        return upd.message.replies

    assert asyncio.run(scenario())[-1] == "😵 The check failed — see the log."


@needs_telegram
def test_telegram_shutdown_does_not_wait_for_a_manual_check(monkeypatch):
    """Regression (F-081): /check ran as an Application.create_task task,
    which Application.stop() awaits, so stopping the bot mid-check waited for
    the whole pass over the feeds."""
    from telegram.ext import ApplicationBuilder

    class _StubBot(telegram.Bot):
        async def initialize(self):
            self._bot_user = telegram.User(id=1, first_name="b", is_bot=True, username="b")
            self._initialized = True

        async def shutdown(self):
            pass

    async def long_check(app, *, trigger):
        await asyncio.Event().wait()  # a pass that outlasts the shutdown

    monkeypatch.setattr(tgbot, "run_check", long_check)

    async def scenario():
        app = ApplicationBuilder().bot(_StubBot("123:abc")).updater(None).build()
        await app.initialize()
        await app.start()
        app.bot_data["notify_chat_id"] = "111"
        ctx = type("C", (), {"application": app, "bot_data": app.bot_data})()
        await tgbot.check_command(_Update(111), ctx)
        await asyncio.sleep(0)
        await asyncio.wait_for(app.stop(), timeout=2)  # used to block here
        task = app.bot_data["check_task"]
        assert not task.done()
        task.cancel()
        await app.shutdown()

    asyncio.run(scenario())


@needs_telegram
def test_telegram_edited_messages_reach_no_handler():
    """Regression (F-079): handlers matched edited messages too, which carry
    no update.message, so editing a sent "/chek" into "/help" crashed the
    handler and the chat got "something went wrong"."""
    bot = telegram.Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot._bot_user = telegram.User(id=1, first_name="b", is_bot=True, username="mybot")

    def update(kind, text):
        entities = (
            [{"type": "bot_command", "offset": 0, "length": len(text)}]
            if text.startswith("/") else []
        )
        return telegram.Update.de_json({"update_id": 1, kind: {
            "message_id": 1, "date": 0, "edit_date": 1, "text": text,
            "chat": {"id": 111, "type": "private"},
            "from": {"id": 111, "is_bot": False, "first_name": "x"},
            "entities": entities,
        }}, bot)

    handlers = tgbot.handlers()
    conv = next(h for h in handlers if isinstance(h, tgbot.ConversationHandler))
    for command in ("/start", "/help", "/check", "/authorize", "/except"):
        assert any(h.check_update(update("message", command)) for h in handlers), command
        assert not any(h.check_update(update("edited_message", command)) for h in handlers), command
    # Mid-conversation, an edited channel name is ignored, a new one is taken.
    conv._conversations[(111, 111)] = tgbot.EXC_CHANNEL
    assert conv.check_update(update("message", "Some Channel"))
    assert not conv.check_update(update("edited_message", "Some Channel"))
    conv._conversations[(111, 111)] = tgbot.EXC_CHANNEL
    assert conv.check_update(update("edited_message", "/cancel")) is None


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


@needs_discord
def test_discord_other_channels_reach_no_handler(monkeypatch):
    """Regression (F-076): no test sent a command or click from outside the
    notify channel, so its gate could be dropped from any of them unnoticed.
    The user here is a server manager, so only the channel check stops them."""
    monkeypatch.setattr(discordbot.config, "DISCORD_CHANNEL_ID", "")
    touched = []
    monkeypatch.setattr(discordbot.youtube_playlist, "add_video", lambda vid: touched.append("save"))
    monkeypatch.setattr(discordbot.youtube_playlist, "is_configured",
                        lambda: touched.append("authorize") or False)
    monkeypatch.setattr(discordbot.discovery, "lookup_subscription",
                        lambda text: touched.append("except") or [])
    client = discordbot.WatchlistClient()
    client.notify_channel_id = "5"

    async def run_check(*, trigger):
        touched.append("check")

    async def begin_authorization(interaction, *, replace=False):
        touched.append("authorize")

    client.run_check = run_check
    client.begin_authorization = begin_authorization
    discordbot._register_commands(client)
    command = lambda name: client.tree.get_command(name).callback
    rule = discordbot.app_commands.Choice(name="mute", value="mute")

    class _Resp(_Response):
        async def defer(self, **kw):
            pass

        async def edit_message(self, **kw):
            touched.append("edit")

    cases = [
        (lambda i: command("check")(i), None),
        (lambda i: command("help")(i), None),
        (lambda i: command("authorize")(i), None),
        (lambda i: command("except")(i, "Some Channel", rule), None),
        (client.on_interaction, f"{discordbot.CB_SAVE}abc"),
        (client.on_interaction, f"{discordbot.CB_DELETE}abc"),
        (client.on_interaction, discordbot.CB_AUTH_YES),
    ]
    for call, custom_id in cases:
        i = _Interaction(9, _Perms(manage_guild=True), HOME_GUILD)
        i.response, i.followup = _Resp(), _Followup()
        i.type = discordbot.discord.InteractionType.component
        i.data = {"custom_id": custom_id}
        i.message = None
        asyncio.run(call(i))
        assert i.response.sent == ["I only work in my notification channel."], custom_id
        assert i.followup.sent == []
    assert touched == []


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


@needs_discord
def test_discord_already_acknowledged_keeps_its_traceback(caplog):
    """Regression (F-068): a 40060 "already acknowledged" error was described
    as a harmless double-click, a text that never reached the log. Answering
    twice is our own bug, so it is logged with its traceback."""
    twice = discordbot.discord.HTTPException(
        _Resp(400, "Bad Request"),
        {"code": 40060, "message": "Interaction has already been acknowledged."},
    )
    assert discordbot.describe_expired_interaction(twice) == ""
    try:
        raise twice
    except discordbot.discord.HTTPException:
        asyncio.run(_client("5").on_error("on_interaction"))
    record = caplog.records[-1]
    assert record.levelname == "ERROR" and record.exc_info is not None


@needs_discord
def test_discord_connection_failure_names_an_asyncio_timeout(monkeypatch):
    """Regression (F-069): before Python 3.11 asyncio.TimeoutError isn't the
    builtin TimeoutError, so a gateway timeout was logged as just
    "TimeoutError". Simulated with a stand-in for the old class."""
    class OldAsyncioTimeout(Exception):
        pass

    monkeypatch.setattr(discordbot.asyncio, "TimeoutError", OldAsyncioTimeout)
    assert "60 seconds" in discordbot.describe_connection_failure(OldAsyncioTimeout())


@needs_discord
def test_discord_guild_id_of_another_server_is_a_clear_error(monkeypatch, caplog, archive_path):
    """Regression (F-070): a numeric DISCORD_GUILD_ID of a server the bot isn't
    in (or a channel's id) made the guild sync's 403 escape main() as a bare
    traceback, on every restart."""
    monkeypatch.setattr(discordbot.config, "DISCORD_BOT_TOKEN", "t")
    monkeypatch.setattr(discordbot.config, "DISCORD_CHANNEL_ID", "")
    monkeypatch.setattr(discordbot.config, "DISCORD_GUILD_ID", "123456789012345678")

    async def refused(*, guild=None):
        raise discordbot.discord.Forbidden(
            _Resp(403, "Forbidden"), {"code": 50001, "message": "Missing Access"}
        )

    class _Client(discordbot.WatchlistClient):
        def run(self, token, **kw):
            self.tree.sync = refused
            asyncio.run(self.setup_hook())  # what Client.login awaits first

    monkeypatch.setattr(discordbot, "WatchlistClient", _Client)
    discordbot.main()  # returns instead of raising
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert errors and "DISCORD_GUILD_ID" in errors[-1] and "Copy Server ID" in errors[-1]


@needs_discord
def test_discord_reconnect_reports_the_outage_once_resumed(caplog):
    """Regression (F-074): nothing checked that the client reports the outage
    when the connection comes back, so on_ready / on_resumed could drop it."""
    import logging
    import socket

    import aiohttp

    dns = _chained(aiohttp.ClientConnectionError("x"), socket.gaierror(-3, "dns"))
    discordbot.reconnect_log.filter(_reconnect_record(dns))
    with caplog.at_level(logging.INFO, logger="ytwatchlistbot"):
        asyncio.run(_client("5").on_resumed())
    assert "Back online" in caplog.records[-1].getMessage()


@needs_discord
def test_discord_command_errors_go_through_the_readable_logger(caplog):
    """Regression (F-074): the slash-command error handler was untested;
    without it an expired /check is logged by discord.py as a traceback."""
    client = discordbot.WatchlistClient()
    discordbot._register_commands(client)
    check = client.tree.get_command("check")
    expired = discordbot.discord.NotFound(
        _Resp(404, "Not Found"), {"code": 10062, "message": "Unknown interaction"}
    )
    interaction = type("I", (), {"command": check})()
    error = discordbot.app_commands.CommandInvokeError(check, expired)
    asyncio.run(client.tree.on_error(interaction, error))
    record = caplog.records[-1]
    assert record.levelname == "WARNING" and record.exc_info is None
    assert "running /check" in record.getMessage()


@needs_telegram
def test_telegram_bad_request_keeps_its_traceback(caplog):
    """Regression (F-071): a BadRequest, usually a malformed message we sent,
    was logged as one line with no traceback, so nothing showed which message
    it was (Discord keeps it). Other known errors stay one line."""
    from telegram.error import BadRequest, TimedOut

    asyncio.run(tgbot.on_error(None, type("C", (), {"error": BadRequest("Can't parse entities")})()))
    record = caplog.records[-1]
    assert record.exc_info is not None and "BadRequest" in record.getMessage()
    asyncio.run(tgbot.on_error(None, type("C", (), {"error": TimedOut()})()))
    assert caplog.records[-1].exc_info is None


# --- The owner's Save / Delete taps ----------------------------------------- #
@needs_telegram
def test_telegram_owner_can_save_and_delete(monkeypatch):
    """Regression (F-087): only strangers' taps were tested, so Save sending
    the wrong video id, or Delete removing nothing, passed every test."""
    saved = []
    monkeypatch.setattr(tgbot.youtube_playlist, "add_video",
                        lambda vid: saved.append(vid) or ("added" if len(saved) == 1 else "exists"))

    class _Query:
        def __init__(self, data):
            self.data = data
            self.answers, self.markups, self.deleted = [], [], False

        async def answer(self, text=None, **kw):
            self.answers.append(text)

        async def edit_message_reply_markup(self, markup=None, **kw):
            self.markups.append(markup)

        async def delete_message(self):
            self.deleted = True

    async def tap(data):
        q = _Query(data)
        upd = type("U", (), {"callback_query": q, "effective_chat": type("C", (), {"id": 111, "type": "private"})()})()
        await tgbot.on_button(upd, _Ctx("111"))
        return q

    async def scenario():
        return (await tap(f"{tgbot.CB_SAVE}dQw4w9WgXcQ"), await tap(f"{tgbot.CB_SAVE}dQw4w9WgXcQ"),
                await tap(f"{tgbot.CB_DELETE}dQw4w9WgXcQ"))

    first, second, delete = asyncio.run(scenario())
    assert saved == ["dQw4w9WgXcQ", "dQw4w9WgXcQ"]
    assert "Added" in first.answers[0] and "Already" in second.answers[0]
    buttons = first.markups[0].inline_keyboard[0]
    assert buttons[0].text == "✅ Saved"
    assert buttons[1].callback_data == f"{tgbot.CB_DELETE}dQw4w9WgXcQ"
    assert delete.deleted and delete.answers == ["🗑 Deleted"]


@needs_telegram
def test_telegram_group_members_who_arent_admins_cannot_control(monkeypatch):
    """Regression (F-090): only the chat was checked, so in a group notify
    chat any member could reconnect YouTube, Save, Delete or add opt-outs.
    Discord already required Manage Server."""
    group_id = -100123
    touched = []
    monkeypatch.setattr(tgbot.youtube_playlist, "add_video",
                        lambda vid: touched.append("save") or "added")
    monkeypatch.setattr(tgbot.youtube_playlist, "is_configured",
                        lambda: touched.append("configured") or False)
    monkeypatch.setattr(tgbot.config, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(tgbot, "_remember_chat", lambda chat: touched.append("start"))

    async def fake_begin(app, chat_id, edit=None):
        touched.append("authorize")

    monkeypatch.setattr(tgbot, "_begin_authorization", fake_begin)

    class _Bot:
        async def get_chat_member(self, chat_id, user_id):
            assert chat_id == group_id
            if user_id == 3:
                raise telegram.error.NetworkError("down")
            status = {1: "creator", 2: "administrator"}.get(user_id, "member")
            return type("Member", (), {"status": status})()

    class _Query:
        def __init__(self, data):
            self.data = data
            self.answers, self.edits = [], []

        async def answer(self, *a, **k):
            self.answers.append(a)

        async def edit_message_text(self, text, **kw):
            self.edits.append(text)

        async def edit_message_reply_markup(self, *a, **k):
            pass

        async def delete_message(self):
            touched.append("delete")

    group = type("C", (), {"id": group_id, "type": "supergroup"})()

    def user(user_id):
        return type("User", (), {"id": user_id})()

    def command(user_id, sender_chat=None):
        upd = _Update(group_id)
        upd.effective_chat, upd.effective_user = group, user(user_id)
        upd.message.sender_chat = sender_chat
        return upd

    def tap(user_id, data):
        q = _Query(data)
        return q, type("U", (), {"callback_query": q, "effective_chat": group,
                                 "message": None, "effective_user": user(user_id)})()

    def ctx(target):
        c = _Ctx(target)
        c.bot, c.user_data = _Bot(), {}
        return c

    async def scenario():
        c = ctx(None)  # nothing registered yet: a member can't claim the group
        await tgbot.start(command(5), c)
        assert c.bot_data["notify_chat_id"] is None
        c = ctx(str(group_id))
        for user_id in (5, 3):  # a plain member; an admin lookup that fails
            for data in (f"{tgbot.CB_SAVE}abc", f"{tgbot.CB_DELETE}abc", f"{tgbot.CB_AUTH}yes"):
                q, upd = tap(user_id, data)
                await (tgbot.on_auth_button if data.startswith(tgbot.CB_AUTH) else tgbot.on_button)(upd, c)
                assert q.answers == [()] and q.edits == []
            for handler in (tgbot.start, tgbot.help_command, tgbot.check_command,
                            tgbot.authorize_command, tgbot.except_start):
                upd = command(user_id)
                await handler(upd, c)
                assert upd.message.replies == [], handler
            await asyncio.sleep(0)
            assert "check_task" not in c.bot_data
        assert touched == []

        q, upd = tap(1, f"{tgbot.CB_SAVE}abc")         # the group's owner
        await tgbot.on_button(upd, c)
        await tgbot.authorize_command(command(2), c)   # an admin
        anonymous = command(1087968824, sender_chat=group)  # admin posting as the group
        await tgbot.start(anonymous, c)
        assert anonymous.message.replies

    asyncio.run(scenario())
    assert touched == ["save", "configured", "authorize", "start"]


@needs_discord
def test_discord_owner_can_save_and_delete(monkeypatch):
    """Regression (F-087): the Discord side of the same gap."""
    monkeypatch.setattr(discordbot.config, "DISCORD_CHANNEL_ID", "")
    saved = []
    monkeypatch.setattr(discordbot.youtube_playlist, "add_video", lambda vid: saved.append(vid) or "added")
    client = discordbot.WatchlistClient()
    client.notify_channel_id = "5"

    class _Resp(_Response):
        async def defer(self, **kw):
            pass

    class _Message:
        def __init__(self):
            self.views, self.deleted = [], False

        async def edit(self, view=None, **kw):
            self.views.append(view)

        async def delete(self):
            self.deleted = True

    def click(custom_id):
        i = _Interaction(5, _Perms(manage_guild=True), HOME_GUILD)
        i.response, i.followup = _Resp(), _Followup()
        i.type = discordbot.discord.InteractionType.component
        i.data = {"custom_id": custom_id}
        i.message = _Message()
        asyncio.run(client.on_interaction(i))
        return i

    save = click(f"{discordbot.CB_SAVE}dQw4w9WgXcQ")
    assert saved == ["dQw4w9WgXcQ"]
    assert [b.custom_id for b in save.message.views[0].children] == [
        discordbot.CB_SAVED, f"{discordbot.CB_DELETE}dQw4w9WgXcQ"]
    assert "Added" in save.followup.sent[0]
    delete = click(f"{discordbot.CB_DELETE}dQw4w9WgXcQ")
    assert delete.message.deleted and delete.followup.sent == []


@needs_discord
def test_discord_save_is_acknowledged_before_asking_youtube(monkeypatch):
    """Regression (F-104): the owner's Save test stubbed defer() as a no-op,
    so answering the click only after YouTube (Discord expires it after 3
    seconds) passed."""
    monkeypatch.setattr(discordbot.config, "DISCORD_CHANNEL_ID", "")
    events = []
    monkeypatch.setattr(discordbot.youtube_playlist, "add_video",
                        lambda vid: events.append("youtube") or "added")
    client = discordbot.WatchlistClient()
    client.notify_channel_id = "5"

    class _Resp(_Response):
        async def defer(self, **kw):
            events.append("ack")

    class _Message:
        async def edit(self, **kw):
            pass

    i = _Interaction(5, _Perms(manage_guild=True), HOME_GUILD)
    i.response, i.followup = _Resp(), _Followup()
    i.type = discordbot.discord.InteractionType.component
    i.data = {"custom_id": f"{discordbot.CB_SAVE}dQw4w9WgXcQ"}
    i.message = _Message()
    asyncio.run(client.on_interaction(i))
    assert events == ["ack", "youtube"]


# --- /except saving --------------------------------------------------------- #
_SUB = {"id": "UC" + "a" * 22, "title": "Some <Channel>"}


@needs_telegram
@pytest.mark.parametrize("choice,line", [
    ("videos", f"{_SUB['id']} = videos  # Some <Channel>"),
    ("livestreams", f"{_SUB['id']} = livestreams  # Some <Channel>"),
    ("mute", f"{_SUB['id']}  # Some <Channel>"),
])
def test_telegram_except_button_writes_the_chosen_rule(monkeypatch, tmp_path, choice, line):
    """Regression (F-091): the type buttons were never tapped in a test, so a
    swapped rule map, or a reply of "Added" with nothing written, passed."""
    exc = tmp_path / "exceptions.txt"
    monkeypatch.setattr(tgbot.config, "EXCEPTIONS_FILE", str(exc))

    class _Query:
        data = f"{tgbot.CB_EXCEPT}{choice}"

        def __init__(self):
            self.edits = []

        async def answer(self, *a, **k):
            pass

        async def edit_message_text(self, text, **kw):
            self.edits.append(text)

    q = _Query()
    ctx = _Ctx("111")
    ctx.user_data = {"exc_channel": dict(_SUB)}
    upd = type("U", (), {"callback_query": q})()
    assert asyncio.run(tgbot.except_type(upd, ctx)) == tgbot.ConversationHandler.END
    assert exc.read_text(encoding="utf-8").splitlines() == [line]
    assert q.edits[0].startswith("✅ Added: <b>Some &lt;Channel&gt;</b>")


@needs_discord
@pytest.mark.parametrize("choice,line", [
    ("videos", f"{_SUB['id']} = videos  # Some <Channel>"),
    ("livestreams", f"{_SUB['id']} = livestreams  # Some <Channel>"),
    ("mute", f"{_SUB['id']}  # Some <Channel>"),
])
def test_discord_except_writes_the_chosen_rule(monkeypatch, tmp_path, choice, line):
    """Regression (F-091): the Discord side of the same gap."""
    exc = tmp_path / "exceptions.txt"
    monkeypatch.setattr(discordbot.config, "EXCEPTIONS_FILE", str(exc))
    monkeypatch.setattr(discordbot.config, "DISCORD_CHANNEL_ID", "")
    monkeypatch.setattr(discordbot.discovery, "lookup_subscription", lambda text: [dict(_SUB)])
    client = discordbot.WatchlistClient()
    client.notify_channel_id = "5"
    discordbot._register_commands(client)

    class _Resp(_Response):
        async def defer(self, **kw):
            pass

    i = _Interaction(5, _Perms(manage_guild=True), HOME_GUILD)
    i.response, i.followup = _Resp(), _Followup()
    rule = discordbot.app_commands.Choice(name=choice, value=choice)
    asyncio.run(client.tree.get_command("except").callback(i, "Some Channel", rule))
    assert exc.read_text(encoding="utf-8").splitlines() == [line]
    assert i.followup.sent[0].startswith("✅ Added:")


# --- What a notification looks like ------------------------------------------ #
_VIDEO = {
    "video_id": "dQw4w9WgXcQ", "title": "Fish & <Chips> *now*", "channel_title": "Chan",
    "link": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "type": "video",
    "published": "2026-07-20T18:00:00+00:00",
}


@needs_telegram
def test_telegram_notifications_carry_link_buttons_and_escaped_text():
    """Regression (F-092): no test looked at a sent notification, so losing the
    link, a Save button that deletes, an unescaped title (Telegram rejects the
    HTML) or a silent set-up message all passed."""
    sent = []

    class _Bot:
        async def send_message(self, chat_id=None, text=None, **kw):
            sent.append((chat_id, text, kw))

    app = type("A", (), {"bot": _Bot(), "bot_data": {"notify_chat_id": "111"}})()
    notifier = tgbot.TelegramNotifier(app)
    asyncio.run(notifier.send_video(dict(_VIDEO)))
    chat_id, text, kw = sent[0]
    assert chat_id == "111" and kw["parse_mode"] == tgbot.ParseMode.HTML
    assert text.splitlines()[-1] == _VIDEO["link"]
    assert "<b>Fish &amp; &lt;Chips&gt; *now*</b>" in text
    save, delete = kw["reply_markup"].inline_keyboard[0]
    assert save.callback_data == f"{tgbot.CB_SAVE}dQw4w9WgXcQ"
    assert delete.callback_data == f"{tgbot.CB_DELETE}dQw4w9WgXcQ"
    asyncio.run(notifier.send_first_run(7))
    assert sent[1][0] == "111" and "Watching <b>7</b> channel(s)" in sent[1][1]


@needs_discord
def test_discord_notifications_carry_link_buttons_and_escaped_text():
    """Regression (F-092): the Discord side of the same gap."""
    sent = []

    class _Channel:
        async def send(self, text, **kw):
            sent.append((text, kw))

    client = type("C", (), {"notify_channel_id": "5",
                            "get_channel": staticmethod(lambda cid: _Channel())})()
    notifier = discordbot.DiscordNotifier(client)
    asyncio.run(notifier.send_video(dict(_VIDEO)))
    text, kw = sent[0]
    assert text.splitlines()[-1] == _VIDEO["link"]
    assert "**Fish & <Chips> \\*now\\***" in text
    assert [b.custom_id for b in kw["view"].children] == [
        f"{discordbot.CB_SAVE}dQw4w9WgXcQ", f"{discordbot.CB_DELETE}dQw4w9WgXcQ"]
    asyncio.run(notifier.send_first_run(7))
    assert "Watching **7** channel(s)" in sent[1][0]


@needs_telegram
def test_telegram_status_messages_are_sent_and_escaped():
    """Regression (F-093): the "now watching" list and the re-authorize alert
    were never looked at, so a no-op send or an unescaped name (Telegram
    rejects the HTML) passed every test."""
    sent = []

    class _Bot:
        async def send_message(self, chat_id=None, text=None, **kw):
            sent.append((chat_id, text, kw.get("parse_mode")))

    app = type("A", (), {"bot": _Bot(), "bot_data": {"notify_chat_id": "111"}})()
    notifier = tgbot.TelegramNotifier(app)
    asyncio.run(notifier.send_seeded(["A <b>", "B & C"]))
    asyncio.run(notifier.send_auth_alert("token <dead>"))
    (c1, seeded, m1), (c2, alert, m2) = sent
    assert c1 == c2 == "111" and m1 == m2 == tgbot.ParseMode.HTML
    assert "Now watching 2 new channel(s)" in seeded
    assert "• A &lt;b&gt;" in seeded and "• B &amp; C" in seeded
    assert "re-authorizing" in alert and "token &lt;dead&gt;" in alert


@needs_discord
def test_discord_status_messages_are_sent():
    """Regression (F-093): the Discord side of the same gap."""
    sent = []

    class _Channel:
        async def send(self, text, **kw):
            sent.append(text)

    client = type("C", (), {"notify_channel_id": "5",
                            "get_channel": staticmethod(lambda cid: _Channel())})()
    notifier = discordbot.DiscordNotifier(client)
    asyncio.run(notifier.send_seeded(["A_b", "C"]))
    asyncio.run(notifier.send_auth_alert("token *dead*"))
    seeded, alert = sent
    assert "Now watching 2 new channel(s)" in seeded and "• A\\_b" in seeded
    assert "re-authorizing" in alert and "token \\*dead\\*" in alert


# --- /except: which channel ------------------------------------------------- #
def test_find_subscriptions_prefers_the_exact_title():
    """Regression (F-094): without the exact-title pass, "Ch1" also matched
    "Ch10", so /except could never pick Ch1."""
    import discovery

    subs = [{"id": "UC1", "title": "Ch1"}, {"id": "UC10", "title": "Ch10"}]
    assert discovery.find_subscriptions("ch1", subs) == [subs[0]]
    assert discovery.find_subscriptions("Ch", subs) == subs


@needs_telegram
def test_telegram_except_remembers_the_matched_channel(monkeypatch):
    """Regression (F-094): the single-match path was untested, so forgetting
    the matched channel (the next button tap then says "Cancelled") passed."""
    monkeypatch.setattr(tgbot.discovery, "lookup_subscription", lambda text: [dict(_SUB)])
    ctx, upd = _Ctx("111"), _Update(111, text="Some Channel")
    ctx.user_data = {}
    assert asyncio.run(tgbot.except_channel(upd, ctx)) == tgbot.EXC_TYPE
    assert ctx.user_data["exc_channel"] == _SUB
    assert "Some &lt;Channel&gt;" in upd.message.replies[0]
    assert upd.message.kwargs[0]["reply_markup"] is not None


# --- A pinned notify target wins at startup --------------------------------- #
@needs_telegram
def test_telegram_pinned_chat_wins_at_startup(monkeypatch, archive_path):
    """Regression (F-095): post_init's choice between TELEGRAM_CHAT_ID and the
    chat /start stored was untested, so ignoring the setting passed."""
    import archive

    archive.set_keys(str(archive_path), notify_chat_id="7")
    monkeypatch.setattr(tgbot.youtube_playlist, "is_configured", lambda: False)

    def app():
        return type("A", (), {"bot_data": {}, "create_task": lambda self, coro: coro.close()})()

    monkeypatch.setattr(tgbot.config, "TELEGRAM_CHAT_ID", "42")
    pinned = app()
    asyncio.run(tgbot.post_init(pinned))
    assert pinned.bot_data["notify_chat_id"] == "42"
    monkeypatch.setattr(tgbot.config, "TELEGRAM_CHAT_ID", "")
    learned = app()
    asyncio.run(tgbot.post_init(learned))
    assert learned.bot_data["notify_chat_id"] == "7"


@needs_discord
def test_discord_pinned_channel_wins_at_startup(monkeypatch, archive_path):
    """Regression (F-095): the Discord side of the same gap (setup_hook)."""
    import archive

    archive.set_keys(str(archive_path), discord_channel_id="9")
    monkeypatch.setattr(discordbot.config, "DISCORD_GUILD_ID", "")

    async def sync(*, guild=None):
        pass

    for pinned, expected in (("5", "5"), ("", "9")):
        monkeypatch.setattr(discordbot.config, "DISCORD_CHANNEL_ID", pinned)
        client = discordbot.WatchlistClient()
        client.tree.sync = sync
        asyncio.run(client.setup_hook())
        assert client.notify_channel_id == expected


# --- The periodic check starts once ----------------------------------------- #
@needs_telegram
def test_telegram_startup_starts_the_check_loop(monkeypatch, archive_path):
    """Regression (F-101): nothing checked that post_init starts the periodic
    check, so dropping it (the bot then never checks) passed."""
    monkeypatch.setattr(tgbot.youtube_playlist, "is_configured", lambda: False)
    started = []
    app = type("A", (), {"bot_data": {},
                         "create_task": lambda self, coro: started.append(coro)})()
    asyncio.run(tgbot.post_init(app))
    assert [c.cr_code.co_name for c in started] == ["check_loop"]
    started[0].close()


@needs_discord
def test_discord_starts_one_check_loop_across_reconnects(monkeypatch):
    """Regression (F-101): on_ready fires again after every reconnect; nothing
    checked that it starts the loop, or that it starts it only once."""
    monkeypatch.setattr(discordbot.youtube_playlist, "is_configured", lambda: True)
    started = []

    class _Loop:
        def create_task(self, coro):
            started.append(coro)
            return coro

    client = discordbot.WatchlistClient()
    client.loop = _Loop()
    client.notify_channel_id = "5"
    asyncio.run(client.on_ready())
    asyncio.run(client.on_ready())  # a reconnect
    assert [c.cr_code.co_name for c in started] == ["check_loop"]
    started[0].close()


def test_add_video_is_idempotent(monkeypatch, tmp_path):
    """Regression (F-087): nothing exercised add_video, so dropping its
    "already in the playlist" check (README: tapping twice never duplicates)
    passed every test. YouTube is stubbed at the REST-helper level."""
    import youtube_playlist

    monkeypatch.setattr(youtube_playlist.config, "YOUTUBE_PLAYLIST_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(youtube_playlist, "_load_credentials", lambda: object())
    playlist, inserts = [], []

    def request(method, path, creds, *, params=None, json_body=None):
        if (method, path) == ("GET", "playlists"):
            return {"items": [{"id": "PL1", "snippet": {"title": youtube_playlist.config.YOUTUBE_PLAYLIST_NAME}}]}
        if (method, path) == ("GET", "playlistItems"):
            return {"items": [{"id": "x"}] if params["videoId"] in playlist else []}
        if (method, path) == ("POST", "playlistItems"):
            inserts.append(json_body["snippet"]["resourceId"]["videoId"])
            playlist.append(inserts[-1])
            return {}
        raise AssertionError((method, path))

    monkeypatch.setattr(youtube_playlist, "_request", request)
    assert youtube_playlist.add_video("dQw4w9WgXcQ") == "added"
    assert youtube_playlist.add_video("dQw4w9WgXcQ") == "exists"
    assert inserts == ["dQw4w9WgXcQ"]
