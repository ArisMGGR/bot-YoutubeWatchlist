"""Configuration loading for YtWatchlistBot.

Values come from environment variables. For local development you can also put
them in a `conf.env` file (see `manual-setup/conf.env.example`); this module loads that file
if it exists without requiring the `python-dotenv` package.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def _path(env_var, default):
    """A file/folder setting. Relative values (and the default) are resolved
    against the code folder, not the current working directory, so the bot
    finds the same files no matter where it was started from. An empty value
    (`ARCHIVE_FILE=`) counts as unset rather than naming the code folder."""
    return os.path.join(HERE, os.environ.get(env_var, "").strip() or default)


def _load_dotenv(path=None):
    path = path or os.path.join(HERE, "conf.env")
    if not os.path.exists(path):
        return
    # utf-8-sig: tolerate a BOM some Windows editors prepend, which would
    # otherwise become part of the first key.
    values = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            # A repeated key: the *last* line wins, as with systemd's
            # EnvironmentFile= (which the service uses) and setup_bot.py.
            values[key.strip()] = val.strip().strip('"').strip("'")
    for key, val in values.items():
        # Don't override variables already set in the real environment.
        os.environ.setdefault(key, val)


def _load_token(env_var, filename):
    """Token from an env var, or from a plain-text file next to the code."""
    token = os.environ.get(env_var, "").strip()
    if token:
        return token
    token_file = os.path.join(HERE, filename)
    if os.path.exists(token_file):
        with open(token_file, encoding="utf-8-sig") as f:
            return f.read().strip()
    return ""


_load_dotenv()

# --- Telegram ---
# Only the bot token is required. TELEGRAM_CHAT_ID is optional: leave it unset
# and the bot learns where to notify from the first /start it receives.
TELEGRAM_BOT_TOKEN = _load_token("TELEGRAM_BOT_TOKEN", "token.txt")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# --- Discord (alternative to Telegram) ---
# Same deal: the token is required, the channel is optional (the bot learns it
# from the first /start it receives in a channel).
DISCORD_BOT_TOKEN = _load_token("DISCORD_BOT_TOKEN", "discord_token.txt")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
# Optional: your server's id. Slash commands registered to one server show up
# immediately; a global registration can take up to an hour to appear.
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "").strip()


def _chat_platform():
    """Which chat platform to run: "telegram" (default) or "discord".

    CHAT_PLATFORM decides. If it isn't set, whichever token you configured
    decides, so a Discord-only setup just works without the extra line.
    """
    choice = os.environ.get("CHAT_PLATFORM", "").strip().lower()
    if choice in ("telegram", "discord"):
        return choice
    if choice:
        print(f"WARNING: unknown CHAT_PLATFORM={choice!r} - using telegram.")
        return "telegram"
    if DISCORD_BOT_TOKEN and not TELEGRAM_BOT_TOKEN:
        return "discord"
    return "telegram"


CHAT_PLATFORM = _chat_platform()

# --- How often the background loop checks channels (minutes) ---
# At least 1: the loop can't check more often, and /help shows this value.
try:
    CHECK_INTERVAL_MINUTES = max(1, int(os.environ.get("CHECK_INTERVAL_MINUTES", "30")))
except ValueError:
    CHECK_INTERVAL_MINUTES = 30

# --- YouTube Data API key (optional) ---
# If provided, new videos are accurately classified as video / live / upcoming /
# livestream and scheduled start times are included. Without it, the bot falls
# back to scraping the watch page (works, slightly less reliable labels).
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()

# --- Saving to a real YouTube playlist (OAuth) ---
# Tapping 💾 Save adds the video to a playlist in your own YouTube account.
# (YouTube's API can't write to the special "Watch Later" list — Google
# disabled that — so we use a normal playlist, which behaves the same.)
# The playlist is auto-created on first use if it doesn't exist yet.
YOUTUBE_PLAYLIST_NAME = (
    os.environ.get("YOUTUBE_PLAYLIST_NAME", "").strip() or "Telegram Watchlist"
)
# One-time OAuth: `client_secret.json` from Google Cloud, and `youtube_token.json`
# produced by `manual-setup/authorize_youtube.py`. `youtube_state.json` caches the playlist id.
YOUTUBE_CLIENT_SECRET_FILE = _path("YOUTUBE_CLIENT_SECRET_FILE", "client_secret.json")
# The device flow (`manual-setup/authorize_youtube.py --device`, handy over SSH) may need a
# separate OAuth client of type "TVs and Limited Input devices". Point this at
# its JSON if so; by default it reuses YOUTUBE_CLIENT_SECRET_FILE.
YOUTUBE_DEVICE_CLIENT_SECRET_FILE = _path(
    "YOUTUBE_DEVICE_CLIENT_SECRET_FILE", YOUTUBE_CLIENT_SECRET_FILE
)
YOUTUBE_TOKEN_FILE = _path("YOUTUBE_TOKEN_FILE", "youtube_token.json")
YOUTUBE_PLAYLIST_STATE_FILE = _path("YOUTUBE_PLAYLIST_STATE_FILE", "youtube_state.json")

# --- Files ---
# The watchlist is your YouTube account's subscriptions (read via OAuth). This
# optional file lets you opt out of channels you're subscribed to, either
# entirely or by notification type — see exceptions.txt for the format.
EXCEPTIONS_FILE = _path("EXCEPTIONS_FILE", "exceptions.txt")
ARCHIVE_FILE = _path("ARCHIVE_FILE", "archive.json")

# --- Logs ---
# Every run appends to a daily plain-text log file under this folder
# (logs/YYYY-MM-DD.log); a new day starts a new file. See logging_setup.py.
LOG_DIR = _path("LOG_DIR", "logs")

# --- Feed fetch retries ---
# YouTube's RSS edge throttles bursts of requests by briefly returning 5xx (and
# spurious 404) errors even for channels that exist. Without retries a single
# blip drops that channel for the whole check. Retry transient failures this
# many times with exponential backoff (FEED_FETCH_BACKOFF is the base delay in
# seconds: 1s, 2s, 4s, ... plus jitter). Set FEED_FETCH_RETRIES to 0 to disable.
# Negative values are clamped to 0 (a negative retry count would otherwise
# leave the fetch with no attempt at all).
try:
    FEED_FETCH_RETRIES = max(0, int(os.environ.get("FEED_FETCH_RETRIES", "3")))
except ValueError:
    FEED_FETCH_RETRIES = 3
try:
    FEED_FETCH_BACKOFF = max(0.0, float(os.environ.get("FEED_FETCH_BACKOFF", "1")))
except ValueError:
    FEED_FETCH_BACKOFF = 1.0

# --- Feed fetch pacing ---
# The check reads every subscription's feed in one pass. Firing those requests
# back-to-back is itself what trips YouTube's burst throttle — the edge starts
# handing back 404/5xx mid-batch, and retrying into an ongoing throttle often
# can't recover in time, so a channel gets skipped for the whole check. Pausing
# this many seconds between feeds keeps the pass under the burst threshold so the
# throttle rarely fires at all. Set to 0 to disable (fetch as fast as possible).
try:
    FEED_FETCH_PACING_SECONDS = max(
        0.0, float(os.environ.get("FEED_FETCH_PACING_SECONDS", "0.5"))
    )
except ValueError:
    FEED_FETCH_PACING_SECONDS = 0.5

# --- Notification freshness guard ---
# YouTube sometimes resurfaces an old video in a channel's RSS feed (e.g. an
# unlisted video flipped to public keeps its original upload date, or a premiere
# / edit). Without a guard the bot would ping you about a video that's weeks old
# just because it hadn't seen that ID before. Skip (but still record) any video
# whose release date is older than this many days. Set to 0 to disable.
try:
    MAX_VIDEO_AGE_DAYS = max(0, int(os.environ.get("MAX_VIDEO_AGE_DAYS", "14")))
except ValueError:
    MAX_VIDEO_AGE_DAYS = 14
