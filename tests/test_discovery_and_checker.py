import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import archive
import checker
import config
import discovery
import youtube
import youtube_playlist


# --- exceptions.txt parsing ------------------------------------------------- #
def test_parse_exceptions_file(tmp_path):
    p = tmp_path / "exceptions.txt"
    p.write_text(
        "# comment\n"
        "\n"
        "@a\n"
        "@b = videos   # inline comment\n"
        "@c = LIVESTREAMS\n"
        "@d = bogus\n"
        "= videos\n"
        "UCxxxxxxxxxxxxxxxxxxxxxx =\n",
        encoding="utf-8",
    )
    rules = discovery.parse_exceptions_file(str(p))
    assert rules == [
        ("@a", discovery.MUTE),
        ("@b", discovery.ONLY_VIDEOS),
        ("@c", discovery.ONLY_LIVE),
        ("@d", discovery.MUTE),  # unknown rule -> safe default
        ("UCxxxxxxxxxxxxxxxxxxxxxx", discovery.MUTE),
    ]


def test_parse_exceptions_file_tolerates_bom(tmp_path):
    """Regression (F-010): a BOM stuck to the first line's channel."""
    p = tmp_path / "exceptions.txt"
    p.write_bytes(b"\xef\xbb\xbf@first = videos\n")
    assert discovery.parse_exceptions_file(str(p)) == [("@first", discovery.ONLY_VIDEOS)]
    assert discovery.set_exception(str(p), "@first", discovery.MUTE) == "updated"


def test_exceptions_share_link_query_is_not_the_rule(tmp_path):
    """Regression (F-063): the `=` in a share link's `?si=...` was taken as the
    rule's, so `<link> = videos` muted the channel instead."""
    p = tmp_path / "exceptions.txt"
    p.write_text(
        "https://www.youtube.com/@SomeChannel?si=AbC123 = videos\n"
        "https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx?si=q\n",
        encoding="utf-8",
    )
    assert discovery.parse_exceptions_file(str(p)) == [
        ("https://www.youtube.com/@SomeChannel", discovery.ONLY_VIDEOS),
        ("https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx", discovery.MUTE),
    ]


def test_find_subscriptions_blank_matches_nothing():
    """Regression (F-008): blank input substring-matched every title."""
    subs = [{"id": "UC1", "title": "A"}, {"id": "UC2", "title": "B"}]
    assert discovery.find_subscriptions("   ", subs) == []


def test_parse_exceptions_file_missing_or_empty(tmp_path):
    assert discovery.parse_exceptions_file(str(tmp_path / "none")) == []
    assert discovery.parse_exceptions_file("") == []


def test_set_exception_adds_then_updates_in_place(tmp_path):
    p = str(tmp_path / "exceptions.txt")
    assert discovery.set_exception(p, "UC1", discovery.MUTE, "Name # x") == "added"
    assert discovery.set_exception(p, "UC2", discovery.ONLY_VIDEOS) == "added"
    assert discovery.set_exception(p, "UC1", discovery.ONLY_LIVE, "Name") == "updated"
    lines = open(p, encoding="utf-8").read().splitlines()
    assert lines == ["UC1 = livestreams  # Name", "UC2 = videos"]
    assert dict(discovery.parse_exceptions_file(p)) == {
        "UC1": discovery.ONLY_LIVE, "UC2": discovery.ONLY_VIDEOS,
    }


def test_rule_allows():
    assert discovery._rule_allows(discovery.ONLY_VIDEOS, "video")
    assert not discovery._rule_allows(discovery.ONLY_VIDEOS, "live")
    assert discovery._rule_allows(discovery.ONLY_LIVE, "upcoming")
    assert not discovery._rule_allows(discovery.ONLY_LIVE, "video")
    assert discovery._rule_allows(None, "video")


def test_is_stale():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=20)).isoformat()
    new = (now - timedelta(days=1)).isoformat()
    assert discovery.is_stale(old, 14)
    assert not discovery.is_stale(new, 14)
    assert not discovery.is_stale(old, 0)
    assert not discovery.is_stale("", 14)
    assert not discovery.is_stale("garbage", 14)


# --- youtube retry helper --------------------------------------------------- #
def test_get_with_retries_zero_retries_returns_response(monkeypatch):
    class Resp:
        status_code = 503

    monkeypatch.setattr(config, "FEED_FETCH_RETRIES", 0)
    monkeypatch.setattr(youtube.session, "get", lambda url, **kw: Resp())
    assert youtube._get_with_retries("http://x").status_code == 503


def test_get_with_retries_retries_then_succeeds(monkeypatch):
    calls = []

    class Resp:
        def __init__(self, code):
            self.status_code = code

    def fake_get(url, **kw):
        calls.append(url)
        return Resp(404 if len(calls) < 3 else 200)

    monkeypatch.setattr(config, "FEED_FETCH_RETRIES", 3)
    monkeypatch.setattr(config, "FEED_FETCH_BACKOFF", 0.0)
    monkeypatch.setattr(youtube.session, "get", fake_get)
    monkeypatch.setattr(youtube.time, "sleep", lambda s: None)
    assert youtube._get_with_retries("http://x").status_code == 200
    assert len(calls) == 3


# --- collect_new_videos + run_check ----------------------------------------- #
FRESH = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


def _feed_stub(per_channel=15, published=FRESH):
    def feed(cid):
        return f"Title {cid}", [
            {"video_id": f"{cid}-v{j}", "title": "t", "link": "l",
             "published": published, "author": "a"}
            for j in range(per_channel)
        ]
    return feed


@pytest.fixture
def offline(monkeypatch):
    """Stub every network call discovery makes."""
    monkeypatch.setattr(discovery, "fetch_channel_feed", _feed_stub())
    monkeypatch.setattr(
        discovery, "classify_videos",
        lambda vs: [v.update(type="video", is_short=False) for v in vs],
    )
    # Bare UC... ids resolve without network; anything else is "unresolvable".
    monkeypatch.setattr(
        discovery, "resolve_channel_id", lambda s: s if s.startswith("UC") else None
    )
    monkeypatch.setattr(config, "FEED_FETCH_PACING_SECONDS", 0)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(checker.asyncio, "sleep", lambda s: real_sleep(0))


def _subs(n):
    return [{"id": f"UC{i:022d}", "title": f"Ch{i}"} for i in range(n)]


def test_first_check_seeds_then_only_new_videos_are_sent(offline, monkeypatch, archive_path, notifier):
    subs = _subs(3)
    monkeypatch.setattr(youtube_playlist, "list_my_subscriptions", lambda: subs)
    asyncio.run(checker.run_check(notifier, {}))
    assert notifier.sent == [("first_run", 3)]
    data = archive.load(str(archive_path))
    assert len(data["seen"]) == 45 and len(data["channels"]) == 3

    def feed_with_one_new(cid):
        title, vids = _feed_stub()(cid)
        if cid == subs[0]["id"]:
            vids.insert(0, {"video_id": "brand-new", "title": "n", "link": "l",
                            "published": FRESH, "author": "a"})
        return title, vids

    monkeypatch.setattr(discovery, "fetch_channel_feed", feed_with_one_new)
    notifier.sent.clear()
    asyncio.run(checker.run_check(notifier, {}))
    assert notifier.sent == [("video", "brand-new")]
    assert archive.load(str(archive_path))["seen"]["brand-new"]["channel_id"] == subs[0]["id"]


def test_overlapping_checks_work_in_more_than_one_event_loop(offline, monkeypatch, archive_path, notifier):
    """Regression (F-040): the check lock was created at import time. Once a
    check had waited for it, it was bound to that event loop, so a check that
    had to wait in another loop (discord.py runs its own; on Python 3.9 even
    the first wait) raised "bound to a different event loop"."""
    monkeypatch.setattr(youtube_playlist, "list_my_subscriptions", lambda: _subs(1))

    async def two_at_once():
        await asyncio.gather(checker.run_check(notifier, {}), checker.run_check(notifier, {}))

    asyncio.run(two_at_once())
    asyncio.run(two_at_once())  # a new loop, and the second check waits again


def test_many_subscriptions_do_not_resurface_after_prune(offline, monkeypatch, archive_path, notifier):
    """Regression for the global 5000-entry cap: 400 × 15 seeded ids must all
    survive, so the second check sends nothing."""
    monkeypatch.setattr(youtube_playlist, "list_my_subscriptions", lambda: _subs(400))
    asyncio.run(checker.run_check(notifier, {}))
    notifier.sent.clear()
    asyncio.run(checker.run_check(notifier, {}))
    assert notifier.sent == []


def test_first_run_message_waits_until_something_was_read(offline, monkeypatch, archive_path, notifier):
    """Regression: a failed subscriptions read on an empty archive used to send
    'set up! Watching 0 channel(s)' on every check."""
    def boom():
        raise youtube_playlist.YouTubeError("403 quotaExceeded")

    monkeypatch.setattr(youtube_playlist, "list_my_subscriptions", boom)
    for _ in range(3):
        asyncio.run(checker.run_check(notifier, {}))
    assert notifier.sent == []

    monkeypatch.setattr(youtube_playlist, "list_my_subscriptions", lambda: _subs(2))
    asyncio.run(checker.run_check(notifier, {}))
    asyncio.run(checker.run_check(notifier, {}))
    assert notifier.sent == [("first_run", 2)]


def test_empty_watchlist_still_gets_the_setup_message(offline, monkeypatch, archive_path, tmp_path, notifier):
    """Regression (F-066): no subscriptions (or all muted) was read as "couldn't
    read any channel", so the set-up message never came; every feed failing
    still waits."""
    monkeypatch.setattr(youtube_playlist, "list_my_subscriptions", lambda: [])
    asyncio.run(checker.run_check(notifier, {}))
    asyncio.run(checker.run_check(notifier, {}))
    assert notifier.sent == [("first_run", 0)]

    subs = _subs(1)
    exc = tmp_path / "exceptions.txt"
    exc.write_text(f"{subs[0]['id']}\n", encoding="utf-8")
    monkeypatch.setattr(config, "EXCEPTIONS_FILE", str(exc))
    monkeypatch.setattr(youtube_playlist, "list_my_subscriptions", lambda: subs)
    _, _, watched = discovery.collect_new_videos(archive.load(str(archive_path)))
    assert watched == 0  # everything muted: nothing to watch, not a failure

    def feed_down(cid):
        raise youtube.requests.ConnectionError("down")

    exc.write_text("", encoding="utf-8")
    monkeypatch.setattr(discovery, "fetch_channel_feed", feed_down)
    _, _, watched = discovery.collect_new_videos(archive.load(str(archive_path)))
    assert watched is None


class _NoChat:
    """A notifier before anyone has sent /start."""
    chat_known = False

    def describe_error(self, exc):
        return ""


def test_setup_message_sent_even_if_start_came_after_first_check(offline, monkeypatch, archive_path, notifier):
    """Regression (F-002): the first check seeded the archive before /start, so
    the "set up" message was never sent once a chat was known."""
    monkeypatch.setattr(youtube_playlist, "list_my_subscriptions", lambda: _subs(2))
    asyncio.run(checker.run_check(_NoChat(), {}))       # before /start
    asyncio.run(checker.run_check(notifier, {}))        # after /start
    asyncio.run(checker.run_check(notifier, {}))
    assert notifier.sent == [("first_run", 2)]


def test_start_during_a_check_survives_the_checks_save(offline, monkeypatch, archive_path):
    """Regression (F-001): /start persisting the chat while a check ran was
    overwritten by the check's save, so the pairing vanished on restart."""
    def subs_then_start():
        archive.set_keys(str(archive_path), notify_chat_id="111")  # /start lands mid-check
        return _subs(1)

    monkeypatch.setattr(youtube_playlist, "list_my_subscriptions", subs_then_start)
    asyncio.run(checker.run_check(_NoChat(), {}))
    assert archive.load(str(archive_path))["notify_chat_id"] == "111"


def test_unmuted_channel_is_reseeded_silently(offline, monkeypatch, archive_path, tmp_path, notifier):
    """Regression (F-019): a channel seeded, muted and later unmuted dumped
    everything it posted while muted."""
    subs = _subs(1)
    exc = tmp_path / "exceptions.txt"
    monkeypatch.setattr(config, "EXCEPTIONS_FILE", str(exc))
    monkeypatch.setattr(youtube_playlist, "list_my_subscriptions", lambda: subs)
    asyncio.run(checker.run_check(notifier, {}))        # seeds it
    exc.write_text(f"{subs[0]['id']}\n", encoding="utf-8")
    asyncio.run(checker.run_check(notifier, {}))        # muted
    assert archive.load(str(archive_path))["channels"] == []

    def feed_with_backlog(cid):
        title, vids = _feed_stub()(cid)
        vids.insert(0, {"video_id": "posted-while-muted", "title": "n", "link": "l",
                        "published": FRESH, "author": "a"})
        return title, vids

    monkeypatch.setattr(discovery, "fetch_channel_feed", feed_with_backlog)
    exc.write_text("", encoding="utf-8")
    notifier.sent.clear()
    asyncio.run(checker.run_check(notifier, {}))        # unmuted
    assert ("video", "posted-while-muted") not in notifier.sent
    assert notifier.sent == [("seeded", [f"Title {subs[0]['id']}"])]


def test_api_error_falls_back_to_scraping(monkeypatch):
    """Regression (F-004): an API error body read as "no items", so every
    video — live streams included — was labelled a regular upload."""
    class Resp:
        ok = False
        status_code = 400

        def json(self):
            return {"error": {"message": "API key not valid"}}

    monkeypatch.setattr(config, "YOUTUBE_API_KEY", "bad")
    monkeypatch.setattr(youtube.session, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(youtube, "_classify_via_scrape", lambda vid: "live")
    monkeypatch.setattr(youtube, "is_short", lambda vid: False)
    videos = [{"video_id": "abc"}]
    youtube.classify_videos(videos)
    assert videos[0]["type"] == "live"


def test_not_authorized_alerts_once_and_again_on_manual(offline, monkeypatch, archive_path, notifier):
    def boom():
        raise youtube_playlist.NotAuthorized("token dead")

    monkeypatch.setattr(youtube_playlist, "list_my_subscriptions", boom)
    state = {}
    asyncio.run(checker.run_check(notifier, state))
    asyncio.run(checker.run_check(notifier, state))
    assert [s[0] for s in notifier.sent] == ["auth"]
    asyncio.run(checker.run_check(notifier, state, trigger="manual"))
    assert [s[0] for s in notifier.sent] == ["auth", "auth"]


def test_playlist_cache_ignored_after_rename(monkeypatch, tmp_path):
    """Regression (F-003): the cached playlist id was reused after
    YOUTUBE_PLAYLIST_NAME changed, so saves kept going to the old playlist."""
    state = tmp_path / "youtube_state.json"
    state.write_text('{"playlist_id": "PL_old", "name": "Old"}', encoding="utf-8")
    monkeypatch.setattr(config, "YOUTUBE_PLAYLIST_STATE_FILE", str(state))
    monkeypatch.setattr(config, "YOUTUBE_PLAYLIST_NAME", "Old")
    assert youtube_playlist._read_playlist_id() == "PL_old"
    monkeypatch.setattr(config, "YOUTUBE_PLAYLIST_NAME", "New")
    assert youtube_playlist._read_playlist_id() is None


def test_malformed_token_file_is_not_authorized(monkeypatch, tmp_path):
    """Regression (F-007): a token file missing fields raised ValueError, which
    the check swallowed as transient — no re-authorize alert ever went out."""
    pytest.importorskip("google.oauth2.credentials")
    token = tmp_path / "youtube_token.json"
    token.write_text('{"token": "x", "client_id": "c"}', encoding="utf-8")
    monkeypatch.setattr(config, "YOUTUBE_TOKEN_FILE", str(token))
    with pytest.raises(youtube_playlist.NotAuthorized):
        youtube_playlist._load_credentials()


def test_refresh_error_names_the_testing_expiry(monkeypatch, tmp_path):
    """Regression (F-061): an OAuth app left in "Testing" gets refresh tokens
    that Google expires after 7 days; the re-authorize alert gave no hint
    why it kept coming back."""
    pytest.importorskip("google.oauth2.credentials")
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    token = tmp_path / "youtube_token.json"
    token.write_text(
        '{"token": "x", "refresh_token": "r", "client_id": "c", '
        '"client_secret": "s", "expiry": "2000-01-01T00:00:00Z"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "YOUTUBE_TOKEN_FILE", str(token))

    def refused(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", refused)
    with pytest.raises(youtube_playlist.NotAuthorized, match='"Testing"'):
        youtube_playlist._load_credentials()


def test_muted_channel_is_not_fetched_and_type_rules_apply(offline, monkeypatch, archive_path, tmp_path):
    subs = _subs(2)
    exc = tmp_path / "exceptions.txt"
    exc.write_text(f"{subs[0]['id']}\n{subs[1]['id']} = livestreams\n", encoding="utf-8")
    monkeypatch.setattr(config, "EXCEPTIONS_FILE", str(exc))
    monkeypatch.setattr(youtube_playlist, "list_my_subscriptions", lambda: subs)
    fetched = []

    def feed(cid):
        fetched.append(cid)
        return _feed_stub()(cid)

    monkeypatch.setattr(discovery, "fetch_channel_feed", feed)
    data = archive.load(str(archive_path))
    data["channels"] = [s["id"] for s in subs]  # already seeded
    videos, seeded, watched = discovery.collect_new_videos(data)
    assert fetched == [subs[1]["id"]]
    assert videos == [] and watched == 1  # all "video" type, channel is livestreams-only
    assert len(data["seen"]) == 15  # dropped items are recorded as seen


def test_api_connection_error_does_not_log_the_key(monkeypatch, caplog):
    """Regression (F-023): a requests error's text carries the request URL,
    key included, and was logged verbatim."""
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError(
            "Max retries exceeded with url: /youtube/v3/videos?id=abc&key=SECRETKEY"
        )

    monkeypatch.setattr(config, "YOUTUBE_API_KEY", "SECRETKEY")
    monkeypatch.setattr(youtube.session, "get", boom)
    monkeypatch.setattr(youtube, "_classify_via_scrape", lambda vid: "video")
    monkeypatch.setattr(youtube, "is_short", lambda vid: False)
    with caplog.at_level("WARNING"):
        youtube.classify_videos([{"video_id": "abc"}])
    assert "falling back to scraping" in caplog.text
    assert "SECRETKEY" not in caplog.text


def test_seeded_listing_is_capped():
    """Regression (F-029): seeding many channels at once built a message over
    the chat platforms' length limits, so it was never delivered."""
    names = [f"Channel {i:03d} " + "x" * 40 for i in range(100)]
    listing = checker.seeded_listing(names, str)
    lines = listing.splitlines()
    assert len(lines) == checker.SEEDED_LIST_MAX + 1
    assert lines[-1] == f"…and {100 - checker.SEEDED_LIST_MAX} more"
    assert checker.seeded_listing(names[:2], str) == f"• {names[0]}\n• {names[1]}"
    # Long names that grow when escaped still fit.
    wide = checker.seeded_listing(["_" * 100] * 100, lambda s: s.replace("_", "\\_"))
    assert len(wide) <= checker.SEEDED_LIST_CHARS + len("\n…and 100 more")
    assert wide.splitlines()[-1].startswith("…and ")
