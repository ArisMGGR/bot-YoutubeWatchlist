"""/authorize: the shared device flow and the Telegram/Discord glue around it.
Google is stubbed at the requests.post level; nothing goes on the network."""
import asyncio
import json
import os

import pytest

import config
import youtube_auth

# The chat glue needs its library; the youtube_auth tests don't, so skip per
# test rather than the whole module (a single-backend install has only one).
try:
    import ytwatchlistbot as tgbot
except ImportError:
    tgbot = None
try:
    import discordbot
except ImportError:
    discordbot = None
needs_telegram = pytest.mark.skipif(tgbot is None, reason="python-telegram-bot not installed")
needs_discord = pytest.mark.skipif(discordbot is None, reason="discord.py not installed")


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.content = b"x"
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.fixture
def secret_file(tmp_path, monkeypatch):
    p = tmp_path / "client_secret.json"
    p.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "sec"}}))
    monkeypatch.setattr(config, "YOUTUBE_DEVICE_CLIENT_SECRET_FILE", str(p))
    return p


@pytest.fixture
def google(monkeypatch):
    """Scripted responses for the device-code and token endpoints."""
    script = {"start": _Resp(200, {
        "user_code": "ABCD-EFGH", "verification_url": "https://www.google.com/device",
        "device_code": "dev", "interval": 5, "expires_in": 1800,
    }), "polls": []}

    def post(url, data=None, timeout=None):
        if url == youtube_auth.DEVICE_CODE_URL:
            return script["start"]
        assert data["device_code"] == "dev" and data["client_secret"] == "sec"
        return script["polls"].pop(0)

    monkeypatch.setattr(youtube_auth.requests, "post", post)
    return script


# --- youtube_auth ----------------------------------------------------------- #
def test_start_returns_flow(secret_file, google):
    flow = youtube_auth.start()
    assert flow.user_code == "ABCD-EFGH" and flow.client_id == "cid"
    assert 1790 < flow.seconds_left <= 1800


def test_start_missing_secret_is_a_friendly_error(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "YOUTUBE_DEVICE_CLIENT_SECRET_FILE", str(tmp_path / "nope.json"))
    with pytest.raises(youtube_auth.DeviceAuthError, match="isn't on the server"):
        youtube_auth.start()


def test_start_client_rejected(secret_file, google):
    google["start"] = _Resp(401, {"error": "invalid_client"})
    with pytest.raises(youtube_auth.ClientRejected, match="TVs and Limited Input"):
        youtube_auth.start()


def test_poll_pending_slow_down_then_token(secret_file, google, monkeypatch, tmp_path):
    flow = youtube_auth.start()
    google["polls"] = [
        _Resp(428, {"error": "authorization_pending"}),
        _Resp(403, {"error": "slow_down"}),
        _Resp(200, {"access_token": "at", "refresh_token": "rt"}),
    ]
    assert youtube_auth.poll(flow) is None
    assert youtube_auth.poll(flow) is None and flow.interval == 10
    creds = youtube_auth.poll(flow)
    assert creds.token == "at" and creds.refresh_token == "rt"

    token = tmp_path / "youtube_token.json"
    state = tmp_path / "youtube_state.json"
    state.write_text('{"playlist_id": "old"}')
    monkeypatch.setattr(config, "YOUTUBE_TOKEN_FILE", str(token))
    monkeypatch.setattr(config, "YOUTUBE_PLAYLIST_STATE_FILE", str(state))
    youtube_auth.save(creds)
    assert json.loads(token.read_text())["refresh_token"] == "rt"
    assert not state.exists()  # playlist cache reset on re-authorization
    assert not os.path.exists(str(token) + ".tmp")


def test_poll_denied_and_expired(secret_file, google):
    flow = youtube_auth.start()
    google["polls"] = [_Resp(403, {"error": "access_denied"})]
    with pytest.raises(youtube_auth.Denied):
        youtube_auth.poll(flow)
    google["polls"] = [_Resp(400, {"error": "expired_token"})]
    with pytest.raises(youtube_auth.Expired):
        youtube_auth.poll(flow)
    flow.expires_at = 0
    with pytest.raises(youtube_auth.Expired):
        youtube_auth.poll(flow)


@pytest.mark.parametrize("content", ['{"installed": {"client_id": "c"', "[1, 2]"])
def test_start_malformed_secret_is_a_friendly_error(monkeypatch, tmp_path, content):
    """Regression (F-030): a truncated or non-object secret file raised a raw
    JSONDecodeError / AttributeError instead of DeviceAuthError."""
    p = tmp_path / "client_secret.json"
    p.write_text(content, encoding="utf-8")
    monkeypatch.setattr(config, "YOUTUBE_DEVICE_CLIENT_SECRET_FILE", str(p))
    with pytest.raises(youtube_auth.DeviceAuthError):
        youtube_auth.start()


def test_wait_honours_cancel(secret_file, google, monkeypatch):
    flow = youtube_auth.start()
    monkeypatch.setattr(youtube_auth.time, "sleep", lambda s: None)
    assert youtube_auth.wait(flow, cancelled=lambda: True) is None


def test_authorize_script_rejects_unknown_flags(capsys):
    """Regression (F-059): unknown arguments were ignored, so `--help` (or a
    typo like `--devcie`) started an authorization."""
    import authorize_youtube

    with pytest.raises(SystemExit) as exc:
        authorize_youtube.parse_args(["--help"])
    assert exc.value.code == 0 and "--device" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        authorize_youtube.parse_args(["--devcie"])
    assert exc.value.code == 2
    assert authorize_youtube.parse_args(["--no-browser"]).console
    assert authorize_youtube.parse_args(["--device"]).device


def test_authorize_bare_pasted_code_is_url_decoded():
    """Regression (F-086): the full URL and `code=...` forms were decoded by
    parse_qs, but a bare code copied out of the address bar (`4%2F0A...`)
    went to Google still encoded."""
    import authorize_youtube

    for pasted in ("http://localhost/?state=s&code=4%2F0AbCd&scope=x",
                   "code=4%2F0AbCd&scope=x", "4%2F0AbCd", "4/0AbCd"):
        assert authorize_youtube._extract_code(pasted) == "4/0AbCd", pasted


def test_authorize_script_survives_a_console_without_emoji(monkeypatch, tmp_path):
    """Regression (F-075): with its output piped or redirected on Windows (the
    ANSI code page, no emoji), "✅ Saved" raised UnicodeEncodeError after the
    token was already written, so a successful authorization ended in a
    traceback and exit status 1."""
    import io
    import sys
    import time

    import authorize_youtube
    import youtube_playlist

    pytest.importorskip("google.oauth2.credentials")  # --device checks for it
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    secret = tmp_path / "client_secret.json"
    secret.write_text("{}", encoding="utf-8")
    token = tmp_path / "youtube_token.json"
    monkeypatch.setattr(config, "YOUTUBE_DEVICE_CLIENT_SECRET_FILE", str(secret))
    monkeypatch.setattr(config, "YOUTUBE_TOKEN_FILE", str(token))
    monkeypatch.setattr(config, "YOUTUBE_PLAYLIST_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(youtube_auth, "start", lambda secret_file: youtube_auth.DeviceFlow(
        "CODE", "https://www.google.com/device", "dev", 0, time.monotonic() + 60, "cid", "sec"))
    creds = type("C", (), {"refresh_token": "r", "to_json": lambda self: '{"refresh_token": "r"}'})()
    monkeypatch.setattr(youtube_auth, "wait", lambda flow: creds)
    monkeypatch.setattr(youtube_playlist, "current_channel", lambda c: {"id": "UC1", "title": "Me 🎮"})

    authorize_youtube.main(["--device"])
    out.flush()
    text = out.buffer.getvalue().decode("cp1252")
    assert token.exists()
    assert "? Saved" in text and "Connected as channel: Me ? (UC1)" in text


# --- Telegram glue ---------------------------------------------------------- #
class _Bot:
    def __init__(self):
        self.sent, self.edited = [], []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return type("M", (), {"message_id": len(self.sent)})()

    async def edit_message_text(self, text, **kw):
        self.edited.append(text)


class _App:
    def __init__(self):
        self.bot = _Bot()
        self.bot_data = {"notify_chat_id": "111"}
        self.tasks = []

    def create_task(self, coro):
        t = asyncio.get_event_loop().create_task(coro)
        self.tasks.append(t)
        return t


class _Ctx:
    def __init__(self, app):
        self.application = app
        self.bot_data = app.bot_data


class _Msg:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def _update(chat_id):
    return type("U", (), {
        "effective_chat": type("C", (), {"id": chat_id, "type": "private"})(), "message": _Msg(),
    })()


@needs_telegram
def test_telegram_authorize_full_flow(secret_file, google, monkeypatch, tmp_path):
    google["polls"] = [
        _Resp(428, {"error": "authorization_pending"}),
        _Resp(200, {"access_token": "at", "refresh_token": "rt"}),
    ]
    token = tmp_path / "youtube_token.json"
    monkeypatch.setattr(config, "YOUTUBE_TOKEN_FILE", str(token))
    monkeypatch.setattr(config, "YOUTUBE_PLAYLIST_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(tgbot.youtube_playlist, "acting_channel", lambda: {"id": "UC1", "title": "Me"})
    checks = []

    async def fake_check(app, *, trigger):
        checks.append(trigger)

    monkeypatch.setattr(tgbot, "run_check", fake_check)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(tgbot.asyncio, "sleep", lambda s: real_sleep(0))

    async def scenario():
        app = _App()
        upd = _update(111)
        await tgbot.authorize_command(upd, _Ctx(app))
        assert "ABCD-EFGH" in app.bot.sent[0] and "google.com/device" in app.bot.sent[0]
        assert app.bot_data["auth_task"] is not None
        await app.bot_data["auth_task"]
        return app

    app = asyncio.run(scenario())
    assert token.exists()
    assert "YouTube connected" in app.bot.edited[-1] and "Me" in app.bot.edited[-1]
    assert checks == ["manual"]
    assert "auth_task" not in app.bot_data


@needs_telegram
def test_telegram_authorize_refuses_strangers_and_asks_when_connected(secret_file, google, monkeypatch):
    monkeypatch.setattr(tgbot.youtube_playlist, "is_configured", lambda: True)
    monkeypatch.setattr(tgbot.youtube_playlist, "acting_channel", lambda: {"id": "UC1", "title": "Me"})

    async def scenario():
        app = _App()
        stranger = _update(999)
        await tgbot.authorize_command(stranger, _Ctx(app))
        assert stranger.message.replies == [] and app.bot.sent == []

        owner = _update(111)
        await tgbot.authorize_command(owner, _Ctx(app))
        assert "currently connected as <b>Me</b>" in owner.message.replies[0]
        assert "auth_task" not in app.bot_data  # nothing started until Yes

    asyncio.run(scenario())


@needs_telegram
def test_telegram_cancel_button_stops_the_wait(secret_file, google, monkeypatch):
    google["polls"] = [_Resp(428, {"error": "authorization_pending"})] * 50

    class _Query:
        def __init__(self, data):
            self.data = data
            self.edits = []

        async def answer(self, *a, **k):
            pass

        async def edit_message_text(self, text, **kw):
            self.edits.append(text)

    async def scenario():
        app = _App()
        await tgbot._begin_authorization(app, 111)
        task = app.bot_data["auth_task"]
        q = _Query(f"{tgbot.CB_AUTH}cancel")
        upd = type("U", (), {"callback_query": q, "effective_chat": type("C", (), {"id": 111, "type": "private"})()})()
        await tgbot.on_auth_button(upd, _Ctx(app))
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()
        assert q.edits == ["✖️ Cancelled — nothing was changed."]
        assert "auth_task" not in app.bot_data

    asyncio.run(scenario())


# --- Discord glue ----------------------------------------------------------- #
@needs_discord
def test_discord_wait_for_approval_saves_and_announces(secret_file, google, monkeypatch, tmp_path):
    google["polls"] = [_Resp(200, {"access_token": "at", "refresh_token": "rt"})]
    token = tmp_path / "youtube_token.json"
    monkeypatch.setattr(config, "YOUTUBE_TOKEN_FILE", str(token))
    monkeypatch.setattr(config, "YOUTUBE_PLAYLIST_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(discordbot.youtube_playlist, "acting_channel", lambda: {"id": "UC1", "title": "Me"})
    real_sleep = asyncio.sleep
    monkeypatch.setattr(discordbot.asyncio, "sleep", lambda s: real_sleep(0))

    client = discordbot.WatchlistClient.__new__(discordbot.WatchlistClient)
    client.notify_channel_id = "5"
    client.check_state = {}
    client._auth_task = None
    posted, edits, checks = [], [], []

    async def run_check(*, trigger):
        checks.append(trigger)

    async def send(self, text, **kw):
        posted.append(text)

    async def edit(**kw):
        edits.append(kw["content"])

    client.run_check = run_check
    monkeypatch.setattr(discordbot.DiscordNotifier, "_send", send)

    flow = youtube_auth.start()
    asyncio.run(client._wait_for_approval(flow, edit))
    assert token.exists()
    assert edits == ["✅ Approved — connected as **Me**."]
    assert posted and "YouTube connected" in posted[0]
    assert checks == ["manual"]
    assert client._auth_task is None


@needs_discord
def test_discord_authorize_before_start_still_runs_the_check(secret_file, google, monkeypatch, tmp_path):
    """Regression (F-067): with no notify channel yet, posting the "YouTube
    connected" notice raised TypeError (int(None)), killing the wait task
    before its check."""
    google["polls"] = [_Resp(200, {"access_token": "at", "refresh_token": "rt"})]
    monkeypatch.setattr(config, "YOUTUBE_TOKEN_FILE", str(tmp_path / "youtube_token.json"))
    monkeypatch.setattr(config, "YOUTUBE_PLAYLIST_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(discordbot.youtube_playlist, "acting_channel", lambda: {"id": "UC1", "title": "Me"})
    real_sleep = asyncio.sleep
    monkeypatch.setattr(discordbot.asyncio, "sleep", lambda s: real_sleep(0))

    client = discordbot.WatchlistClient.__new__(discordbot.WatchlistClient)
    client.notify_channel_id = None  # /start never run
    client._auth_task = None
    checks, edits = [], []

    async def run_check(*, trigger):
        checks.append(trigger)

    async def edit(**kw):
        edits.append(kw["content"])

    client.run_check = run_check
    asyncio.run(client._wait_for_approval(youtube_auth.start(), edit))
    assert edits == ["✅ Approved — connected as **Me**."]
    assert checks == ["manual"]


@needs_discord
def test_discord_concurrent_yes_starts_one_flow(monkeypatch):
    """Regression (F-011, Discord): the pending-flow check ran before awaiting
    youtube_auth.start, so two Yes clicks inside that round-trip both started
    a device flow, and only one of them could be cancelled."""
    starts = []

    def slow_start(secret_file=None):
        starts.append(1)
        import time
        time.sleep(0.2)
        return youtube_auth.DeviceFlow("CODE", "https://www.google.com/device", "dev",
                                       5, time.monotonic() + 1800, "cid", "sec")

    monkeypatch.setattr(youtube_auth, "start", slow_start)
    monkeypatch.setattr(discordbot.WatchlistClient, "loop",
                        property(lambda self: asyncio.get_running_loop()), raising=False)

    class _Response:
        def __init__(self, contents):
            self.contents = contents

        async def defer(self, **kw):
            await asyncio.sleep(0)  # a real round trip yields to the other click

        async def edit_message(self, content=None, view=None):
            self.contents.append(content)

    manager = type("U", (), {"guild_permissions": type("P", (), {
        "manage_guild": True, "administrator": False})()})()

    class _Interaction:
        type = discordbot.discord.InteractionType.component
        channel_id = 5
        guild_id = 100
        user = manager
        data = {"custom_id": discordbot.CB_AUTH_YES}

        def __init__(self):
            self.contents = []
            self.response = _Response(self.contents)

        async def edit_original_response(self, content=None, view=None):
            self.contents.append(content)

    async def scenario():
        client = discordbot.WatchlistClient.__new__(discordbot.WatchlistClient)
        client.notify_channel_id = "5"
        client._auth_task = None

        async def wait_forever(flow, edit):
            await asyncio.sleep(3600)

        client._wait_for_approval = wait_forever
        first, second = _Interaction(), _Interaction()
        await asyncio.gather(client.on_interaction(first), client.on_interaction(second))
        assert client.auth_in_progress()
        client._auth_task.cancel()
        return first.contents + second.contents

    replies = asyncio.run(scenario())
    assert len(starts) == 1
    assert any("already waiting" in (r or "") for r in replies)


@needs_discord
def test_discord_yes_is_acknowledged_before_asking_google(monkeypatch):
    """Regression (F-044): the Yes click was first answered only after Google
    returned a code, so a reply slower than Discord's 3-second window made
    the click fail."""
    events = []

    def start(secret_file=None):
        events.append("google")
        return youtube_auth.DeviceFlow("CODE", "https://www.google.com/device", "dev",
                                       5, 10**9, "cid", "sec")

    monkeypatch.setattr(youtube_auth, "start", start)
    monkeypatch.setattr(discordbot.WatchlistClient, "loop",
                        property(lambda self: asyncio.get_running_loop()), raising=False)

    class _Response:
        async def defer(self, **kw):
            events.append("ack")

        async def edit_message(self, **kw):
            events.append("ack")

    class _Interaction:
        response = _Response()

        async def edit_original_response(self, **kw):
            events.append("code shown")

    async def scenario():
        client = discordbot.WatchlistClient.__new__(discordbot.WatchlistClient)
        client._auth_task = None

        async def wait_forever(flow, edit):
            await asyncio.sleep(3600)

        client._wait_for_approval = wait_forever
        await client.begin_authorization(_Interaction(), replace=True)
        client._auth_task.cancel()

    asyncio.run(scenario())
    assert events == ["ack", "google", "code shown"]


def test_failed_token_write_keeps_the_old_token(tmp_path):
    """Regression (F-083): the token write was checked only for a leftover
    .tmp, which an in-place write also passes. A write that fails midway (a
    lone surrogate can't be encoded) must leave the previous token intact."""
    import youtube_playlist

    token = tmp_path / "youtube_token.json"
    youtube_playlist.write_private(str(token), '{"refresh_token": "r1"}')
    with pytest.raises(UnicodeEncodeError):
        youtube_playlist.write_private(str(token), '{"refresh_token": "\ud800"}')
    assert json.loads(token.read_text(encoding="utf-8")) == {"refresh_token": "r1"}


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX-only")
def test_saved_token_file_is_private(monkeypatch, tmp_path):
    """Regression (F-065): youtube_token.json (refresh token + client secret)
    was written with the umask's 0644, readable by every local user — both by
    the authorization and by every later token refresh."""
    import youtube_playlist

    token = tmp_path / "youtube_token.json"
    monkeypatch.setattr(config, "YOUTUBE_TOKEN_FILE", str(token))
    monkeypatch.setattr(config, "YOUTUBE_PLAYLIST_STATE_FILE", str(tmp_path / "state.json"))
    old_umask = os.umask(0o022)
    try:
        youtube_auth.save(type("C", (), {"to_json": lambda self: '{"refresh_token": "r"}'})())
        assert token.stat().st_mode & 0o777 == 0o600
        token.chmod(0o644)  # a token saved before this fix
        (tmp_path / "youtube_token.json.tmp").write_text("{}")  # leftover temp file
        youtube_playlist.write_private(str(token), '{"refresh_token": "r2"}')
        assert token.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old_umask)
