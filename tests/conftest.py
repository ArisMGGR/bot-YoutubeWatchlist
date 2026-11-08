"""Shared fixtures. Everything runs offline: config is pointed at a temp folder
before any project module is imported, and no test touches the network."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# The setup scripts (setup_bot.py, authorize_youtube.py) live in manual-setup/.
sys.path.insert(1, os.path.join(ROOT, "manual-setup"))

# config.py reads these at import time, so set them before importing it.
_SCRATCH = tempfile.mkdtemp(prefix="ytw-tests-")
os.environ.setdefault("ARCHIVE_FILE", os.path.join(_SCRATCH, "archive.json"))
os.environ.setdefault("EXCEPTIONS_FILE", os.path.join(_SCRATCH, "exceptions.txt"))
os.environ.setdefault("LOG_DIR", os.path.join(_SCRATCH, "logs"))
os.environ.setdefault("YOUTUBE_TOKEN_FILE", os.path.join(_SCRATCH, "youtube_token.json"))
os.environ.setdefault("FEED_FETCH_PACING_SECONDS", "0")
os.environ.pop("TELEGRAM_CHAT_ID", None)
os.environ.pop("DISCORD_CHANNEL_ID", None)

# config.py also loads the project's own conf.env at import, for any key not
# already in the environment. On a configured install that would leak the real
# settings (MAX_VIDEO_AGE_DAYS, API key, chat ids ...) into the tests, so claim
# every key it sets first, as "" — which config.py reads as "unset".
_CONF = os.path.join(ROOT, "conf.env")
if os.path.exists(_CONF):
    with open(_CONF, encoding="utf-8-sig") as _f:
        for _line in _f:
            _line = _line.strip()
            _key = _line.partition("=")[0].strip()
            if _key and not _line.startswith("#") and "=" in _line:
                os.environ.setdefault(_key, "")

import pytest  # noqa: E402

import config  # noqa: E402


@pytest.fixture
def archive_path(tmp_path, monkeypatch):
    """A fresh archive file for one test."""
    path = tmp_path / "archive.json"
    monkeypatch.setattr(config, "ARCHIVE_FILE", str(path))
    return path


class FakeNotifier:
    """Records what the checker asked it to send."""

    chat_known = True

    def __init__(self):
        self.sent = []

    async def send_video(self, v):
        self.sent.append(("video", v["video_id"]))

    async def send_first_run(self, watched):
        self.sent.append(("first_run", watched))

    async def send_seeded(self, names):
        self.sent.append(("seeded", list(names)))

    async def send_auth_alert(self, reason):
        self.sent.append(("auth", reason))

    def describe_error(self, exc):
        return ""


@pytest.fixture
def notifier():
    return FakeNotifier()
