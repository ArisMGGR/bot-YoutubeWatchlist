"""Add saved videos to a real playlist in your YouTube account.

Tapping 💾 Save calls :func:`add_video`, which inserts the video into a normal
playlist in your own account (auto-created the first time). This uses OAuth —
the bot acts *as you* — so you must authorize it once with
``python manual-setup/authorize_youtube.py`` before Save can work (see the README).

Why a normal playlist and not "Watch Later": YouTube's Data API cannot write to
the special *Watch Later* or *History* playlists (Google removed that in 2016).
A normal playlist lives in the same account and behaves the same way.

Runtime is intentionally light: `google-auth` handles the OAuth token (and its
refresh); the two or three API calls are plain HTTPS requests. All of it is
blocking, so the bot calls :func:`add_video` from a worker thread.
"""
import json
import logging
import os
import threading

import requests

import config

log = logging.getLogger("ytwatchlistbot.youtube")

# Managing playlists needs this scope. Kept in sync with manual-setup/authorize_youtube.py.
SCOPES = ["https://www.googleapis.com/auth/youtube"]

API_BASE = "https://www.googleapis.com/youtube/v3"

# Serialize credential refresh / playlist creation across Save taps and the
# periodic check (both can trigger a token refresh from their own worker
# thread). Re-entrant because add_video holds it while calling helpers that
# take it themselves.
_LOCK = threading.RLock()


def write_private(path, text):
    """Write `text` to `path` via a temp file + rename, so a crash mid-write
    never leaves a truncated (unreadable) file behind. The file holds the
    YouTube token, so only this user may read it (0600; the mode does nothing
    on Windows)."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.chmod(tmp, 0o600)  # a leftover .tmp would keep its old mode
        f.write(text)
    os.replace(tmp, path)


class NotAuthorized(Exception):
    """Raised when YouTube hasn't been connected yet (no/invalid token)."""


class YouTubeError(Exception):
    """Raised when a YouTube API call fails for any other reason."""


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
def _load_credentials():
    """Return valid OAuth credentials, refreshing (and re-saving) if needed."""
    with _LOCK:
        return _load_credentials_locked()


def _load_credentials_locked():
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google.auth.exceptions import RefreshError
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise NotAuthorized(
            "google-auth isn't installed — run: pip install -r requirements.txt"
        ) from exc

    if not os.path.exists(config.YOUTUBE_TOKEN_FILE):
        raise NotAuthorized(
            "YouTube isn't connected yet. Run `python manual-setup/authorize_youtube.py` once "
            "(see the README) to grant access."
        )

    try:
        creds = Credentials.from_authorized_user_file(
            config.YOUTUBE_TOKEN_FILE, SCOPES
        )
    except (ValueError, KeyError) as exc:  # bad JSON or missing fields
        raise NotAuthorized(
            "YouTube token file is unreadable or incomplete. Re-run "
            "`python manual-setup/authorize_youtube.py` (or send /authorize)."
        ) from exc
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                raise NotAuthorized(
                    "YouTube access was revoked or expired. Re-run "
                    "`python manual-setup/authorize_youtube.py` to reconnect. "
                    "If this happens every 7 days, your Google OAuth app is "
                    "still in \"Testing\" — publish it (see the README)."
                ) from exc
            write_private(config.YOUTUBE_TOKEN_FILE, creds.to_json())
        else:
            raise NotAuthorized(
                "YouTube token is invalid. Re-run `python manual-setup/authorize_youtube.py`."
            )
    return creds


# --------------------------------------------------------------------------- #
# Thin REST helpers
# --------------------------------------------------------------------------- #
def _request(method, path, creds, *, params=None, json_body=None):
    headers = {"Authorization": f"Bearer {creds.token}"}
    resp = requests.request(
        method, f"{API_BASE}/{path}",
        headers=headers, params=params, json=json_body, timeout=30,
    )
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", {})
            reason = (err.get("errors") or [{}])[0].get("reason", "")
            message = err.get("message", resp.text)
        except ValueError:
            reason, message = "", resp.text
        raise YouTubeError(f"{resp.status_code} {reason}: {message}")
    return resp.json() if resp.content else {}


# --------------------------------------------------------------------------- #
# Playlist id (cached in a small state file)
# --------------------------------------------------------------------------- #
def _read_playlist_id():
    """The cached playlist id — only if it was cached for the playlist name
    configured now, so renaming YOUTUBE_PLAYLIST_NAME takes effect."""
    try:
        with open(config.YOUTUBE_PLAYLIST_STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("name") != config.YOUTUBE_PLAYLIST_NAME:
        return None
    return state.get("playlist_id")


def _write_playlist_id(playlist_id):
    write_private(
        config.YOUTUBE_PLAYLIST_STATE_FILE,
        json.dumps({"playlist_id": playlist_id, "name": config.YOUTUBE_PLAYLIST_NAME}),
    )


def _find_playlist_by_name(creds, name):
    """Return the id of the caller's playlist titled `name`, or None."""
    params = {"part": "snippet", "mine": "true", "maxResults": 50}
    while True:
        data = _request("GET", "playlists", creds, params=params)
        for item in data.get("items", []):
            if item["snippet"]["title"] == name:
                return item["id"]
        token = data.get("nextPageToken")
        if not token:
            return None
        params["pageToken"] = token


def _create_playlist(creds, name):
    body = {
        "snippet": {
            "title": name,
            "description": (
                f"Videos saved from YtWatchlistBot on {config.CHAT_PLATFORM.title()}."
            ),
        },
        "status": {"privacyStatus": "private"},
    }
    data = _request(
        "POST", "playlists", creds,
        params={"part": "snippet,status"}, json_body=body,
    )
    return data["id"]


def _ensure_playlist(creds):
    """Return the target playlist id, finding or creating it as needed."""
    playlist_id = _read_playlist_id()
    if playlist_id:
        return playlist_id
    name = config.YOUTUBE_PLAYLIST_NAME
    playlist_id = _find_playlist_by_name(creds, name) or _create_playlist(creds, name)
    _write_playlist_id(playlist_id)
    log.info("Using YouTube playlist %r (%s).", name, playlist_id)
    return playlist_id


def _already_in_playlist(creds, playlist_id, video_id):
    data = _request(
        "GET", "playlistItems", creds,
        params={"part": "id", "playlistId": playlist_id,
                "videoId": video_id, "maxResults": 1},
    )
    return bool(data.get("items"))


def _insert(creds, playlist_id, video_id):
    body = {
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        }
    }
    _request("POST", "playlistItems", creds,
             params={"part": "snippet"}, json_body=body)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def is_configured():
    """True if a token file exists (so Save can at least attempt YouTube)."""
    return os.path.exists(config.YOUTUBE_TOKEN_FILE)


def add_video(video_id):
    """Add `video_id` to the target playlist.

    Returns "added" or "exists" (already there — Save stays idempotent). Raises
    NotAuthorized if YouTube isn't connected, or YouTubeError on API failure.
    Blocking — call from a worker thread.
    """
    with _LOCK:
        creds = _load_credentials()
        playlist_id = _ensure_playlist(creds)
        try:
            if _already_in_playlist(creds, playlist_id, video_id):
                return "exists"
            _insert(creds, playlist_id, video_id)
        except YouTubeError as exc:
            # A cached playlist that was deleted server-side: forget it, recreate,
            # and retry once so a stale id doesn't wedge every future Save.
            if "playlistNotFound" in str(exc) or "404" in str(exc):
                log.warning("Cached playlist missing (%s) — recreating.", exc)
                try:
                    os.remove(config.YOUTUBE_PLAYLIST_STATE_FILE)
                except OSError:
                    pass
                playlist_id = _ensure_playlist(creds)
                if _already_in_playlist(creds, playlist_id, video_id):
                    return "exists"
                _insert(creds, playlist_id, video_id)
            else:
                raise
        return "added"


def playlist_url(playlist_id):
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def list_my_subscriptions():
    """Return ``[{'id': 'UC...', 'title': ...}, ...]`` for the authorized account.

    These are the channels the connected YouTube account subscribes to — the
    bot's watchlist. The ``youtube`` scope Save already uses grants read access
    to your own subscriptions (``mine=true``), so no extra authorization is
    needed; a channel hidden from the *public* is still visible to you here.

    Raises :class:`NotAuthorized` if YouTube isn't connected, or
    :class:`YouTubeError` on an API failure. Blocking — call from a worker
    thread.
    """
    creds = _load_credentials()
    subs = []
    params = {
        "part": "snippet",
        "mine": "true",
        "maxResults": 50,
        "order": "alphabetical",
    }
    while True:
        data = _request("GET", "subscriptions", creds, params=params)
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            cid = (snippet.get("resourceId") or {}).get("channelId")
            if cid:
                subs.append({"id": cid, "title": snippet.get("title", "")})
        token = data.get("nextPageToken")
        if not token:
            return subs
        params["pageToken"] = token


def current_channel(creds):
    """Return {'id', 'title'} of the channel this token acts as, or None.

    A Google login can own several channels (a default one plus any brand
    accounts). The token is bound to whichever channel was picked during
    authorization — this reports which, so you can tell you connected the right
    one (e.g. a brand channel rather than the personal default).
    """
    data = _request("GET", "channels", creds, params={"part": "snippet", "mine": "true"})
    items = data.get("items") or []
    if not items:
        return None
    return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}


def acting_channel():
    """current_channel() for the stored token. Raises NotAuthorized if unlinked."""
    return current_channel(_load_credentials())


# --------------------------------------------------------------------------- #
# Self-test CLI:  python youtube_playlist.py [VIDEO_ID]
# --------------------------------------------------------------------------- #
def _hint(err):
    """Print an actionable tip for the common failures."""
    s = str(err)
    if "youtubeSignupRequired" in s:
        print(
            "   → This Google account has no YouTube channel yet. Open "
            "youtube.com signed in as this account, create a channel, then retry."
        )
    elif "quotaExceeded" in s or "dailyLimitExceeded" in s:
        print("   → YouTube API quota is used up for today; try again tomorrow.")
    elif "accessNotConfigured" in s or "SERVICE_DISABLED" in s:
        print("   → Enable 'YouTube Data API v3' for this project in the Cloud Console.")
    elif "insufficient" in s.lower() or "forbidden" in s.lower() or "403" in s:
        print(
            "   → The token may be missing the needed scope. Delete "
            "youtube_token.json and re-run: python manual-setup/authorize_youtube.py"
        )


def parse_args(argv=None):
    """Command-line options. Unknown ones are an error, so `--help` (or a typo)
    never reaches YouTube as a video id."""
    import argparse
    import re

    parser = argparse.ArgumentParser(
        description="Self-test: check the YouTube connection and the Save "
        "playlist, and optionally add one video to it.",
        epilog="An id that starts with '-' goes after '--', e.g. "
        "python youtube_playlist.py -- -abcdefghij",
    )
    parser.add_argument(
        "video_id", nargs="?",
        help="a video id to add to the playlist (11 characters, e.g. dQw4w9WgXcQ)",
    )
    args = parser.parse_args(argv)
    # Spelled out, not \w: that also matches non-ASCII letters and digits.
    if args.video_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{11}", args.video_id):
        parser.error(f"{args.video_id!r} isn't a YouTube video id (11 characters).")
    return args


def main(argv=None):
    import sys

    args = parse_args(argv)
    # Output piped or redirected on Windows uses the ANSI code page, which has
    # no emoji: show "?" instead of crashing on the first status line.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    try:
        creds = _load_credentials()
    except NotAuthorized as exc:
        print(f"❌ {exc}")
        return 1

    try:
        channel = current_channel(creds)
    except YouTubeError as exc:
        print(f"❌ Couldn't read the connected channel: {exc}")
        _hint(exc)
        return 1
    if channel:
        print(f"🔗 Connected as channel: {channel['title']} ({channel['id']})")
        print("   If that's the wrong channel, delete youtube_token.json and")
        print("   youtube_state.json, re-run manual-setup/authorize_youtube.py, and pick the")
        print("   right channel at Google's 'Choose a channel' screen.")
    else:
        print("⚠️ This Google account has no YouTube channel.")

    try:
        playlist_id = _ensure_playlist(creds)
    except YouTubeError as exc:
        print(f"❌ Couldn't create or find the playlist: {exc}")
        _hint(exc)
        return 1

    print(f"✅ Playlist ready: '{config.YOUTUBE_PLAYLIST_NAME}'")
    print(f"   {playlist_url(playlist_id)}")
    print("   (It's private — find it under Library → Playlists on YouTube.)")

    if args.video_id:
        video_id = args.video_id
        try:
            result = add_video(video_id)
        except (NotAuthorized, YouTubeError) as exc:
            print(f"❌ Adding {video_id} failed: {exc}")
            _hint(exc)
            return 1
        print(f"✅ Video {video_id}: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
