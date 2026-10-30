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


def test_exceptions_file_that_isnt_utf8_still_works(offline, monkeypatch, archive_path, tmp_path, notifier, caplog):
    """Regression (F-085): one byte from a non-UTF-8 editor (Windows "ANSI")
    raised UnicodeDecodeError, so every check failed and /except couldn't
    save. Now it is logged and read, and /except keeps those bytes as they
    were."""
    subs = _subs(2)
    exc = tmp_path / "exceptions.txt"
    exc.write_bytes(f"{subs[0]['id']}  # Café\n".encode("cp1252"))
    monkeypatch.setattr(config, "EXCEPTIONS_FILE", str(exc))
    monkeypatch.setattr(youtube_playlist, "list_my_subscriptions", lambda: subs)
    with caplog.at_level("WARNING"):
        asyncio.run(checker.run_check(notifier, {}))
    assert notifier.sent == [("first_run", 1)]  # the muted channel stays muted
    assert "isn't UTF-8" in caplog.text
    assert discovery.set_exception(str(exc), subs[1]["id"], discovery.ONLY_VIDEOS, "B") == "added"
    written = exc.read_bytes().replace(b"\r\n", b"\n")  # text mode on Windows
    assert written == f"{subs[0]['id']}  # Café\n{subs[1]['id']} = videos  # B\n".encode("cp1252")


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


# --- youtube.py: feed, Shorts, API labels ----------------------------------- #
# Everything below the module boundary is stubbed in the check tests, so these
# run youtube.py itself against canned HTTP answers (F-088).
_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <title>Some Channel</title>
 <entry>
  <id>yt:video:dQw4w9WgXcQ</id>
  <yt:videoId>dQw4w9WgXcQ</yt:videoId>
  <yt:channelId>UCxxxxxxxxxxxxxxxxxxxxxx</yt:channelId>
  <title>A title</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=dQw4w9WgXcQ"/>
  <author><name>Some Channel</name></author>
  <published>2026-07-20T18:00:00+00:00</published>
 </entry>
</feed>"""


def test_fetch_channel_feed_parses_youtubes_atom(monkeypatch):
    """Regression (F-088): the feed parser never ran in a test, so a wrong
    namespace returned no videos — and no notifications — with the suite green."""
    class Resp:
        status_code = 200
        content = _ATOM

        def raise_for_status(self):
            pass

    monkeypatch.setattr(youtube.session, "get", lambda url, **kw: Resp())
    title, videos = youtube.fetch_channel_feed("UCxxxxxxxxxxxxxxxxxxxxxx")
    assert title == "Some Channel"
    assert videos == [{
        "video_id": "dQw4w9WgXcQ", "title": "A title",
        "link": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "published": "2026-07-20T18:00:00+00:00", "author": "Some Channel",
    }]


def test_is_short_tells_shorts_from_redirected_videos(monkeypatch):
    """Regression (F-088): a regular video's /shorts/ URL redirects to /watch,
    which also answers 200. Following that redirect would call every video a
    Short and drop it silently."""
    def head(url, allow_redirects=False, **kw):
        status = 200 if "SHORTxxxxxx" in url or allow_redirects else 303
        return type("R", (), {"status_code": status})()

    monkeypatch.setattr(youtube.session, "head", head)
    assert youtube.is_short("SHORTxxxxxx")
    assert not youtube.is_short("dQw4w9WgXcQ")


def test_api_classification_labels_each_kind(monkeypatch):
    """Regression (F-088): only the API's error path was tested; its mapping to
    live / upcoming / ended stream / video was not."""
    items = [
        {"id": "live1", "snippet": {"liveBroadcastContent": "live"}, "liveStreamingDetails": {}},
        {"id": "soon1", "snippet": {"liveBroadcastContent": "upcoming"},
         "liveStreamingDetails": {"scheduledStartTime": "2026-07-20T18:00:00Z"}},
        {"id": "ended1", "snippet": {"liveBroadcastContent": "none"},
         "liveStreamingDetails": {"actualEndTime": "2026-07-19T18:00:00Z"}},
        {"id": "plain1", "snippet": {"liveBroadcastContent": "none"}},
    ]

    class Resp:
        ok = True
        status_code = 200

        def json(self):
            return {"items": items}

    monkeypatch.setattr(config, "YOUTUBE_API_KEY", "k")
    monkeypatch.setattr(youtube.session, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(youtube, "is_short", lambda vid: False)
    videos = [{"video_id": i["id"]} for i in items]
    youtube.classify_videos(videos)
    assert [v["type"] for v in videos] == ["live", "upcoming", "livestream", "video"]
    assert videos[1]["scheduled_start"] == "2026-07-20 18:00 UTC"


def test_scrape_classification_labels_each_kind(monkeypatch):
    """Regression (F-089): with no API key every video is labelled from its
    watch page, and that parser never ran in a test."""
    pages = {
        "live1": '"isLiveContent":true,"isLiveNow":true',
        "soon1": '"isLiveContent":true,"isUpcoming":true',
        "ended1": '"isLiveContent":true,"isLiveNow":false',
        "plain1": '"isLiveContent":false',
    }

    def get(url, **kw):
        return type("R", (), {"text": pages[url.rsplit("=", 1)[1]]})()

    monkeypatch.setattr(config, "YOUTUBE_API_KEY", "")
    monkeypatch.setattr(youtube.session, "get", get)
    monkeypatch.setattr(youtube, "is_short", lambda vid: False)
    videos = [{"video_id": vid} for vid in pages]
    youtube.classify_videos(videos)
    assert [v["type"] for v in videos] == ["live", "upcoming", "livestream", "video"]


def test_resolve_channel_id_picks_the_pages_own_channel(monkeypatch):
    """Regression (F-089): a channel page names other channels too (featured,
    video owners) before its own canonical link; trying the generic patterns
    first would put the opt-out on the wrong channel."""
    own, other = "UC" + "a" * 22, "UC" + "b" * 22
    page = (f'"channelId":"{other}" <a href="https://www.youtube.com/channel/{other}">'
            f'<link rel="canonical" href="https://www.youtube.com/channel/{own}">')
    asked = []

    def get(url, **kw):
        asked.append(url)
        return type("R", (), {"status_code": 404 if "gone" in url else 200, "text": page})()

    monkeypatch.setattr(youtube.session, "get", get)
    assert youtube.resolve_channel_id("@Some") == own
    assert youtube.resolve_channel_id("Some") == own
    assert youtube.resolve_channel_id("https://www.youtube.com/c/Some") == own
    assert youtube.resolve_channel_id("@gone") is None
    assert asked == ["https://www.youtube.com/@Some"] * 2 + [
        "https://www.youtube.com/c/Some", "https://www.youtube.com/@gone"]
    # An id, or a /channel/ link, needs no request.
    assert youtube.resolve_channel_id(other) == other
    assert youtube.resolve_channel_id(f"https://youtube.com/channel/{other}?si=x") == other
    assert len(asked) == 4


class _YouTubeAPI:
    """Canned answers for youtube_playlist's REST calls, by method + path."""

    def __init__(self, monkeypatch, answers):
        self.answers, self.calls = answers, []
        monkeypatch.setattr(youtube_playlist, "_load_credentials",
                            lambda: type("Creds", (), {"token": "t"})())
        monkeypatch.setattr(youtube_playlist.requests, "request", self.request)

    def request(self, method, url, params=None, json=None, **kw):
        path = url.rsplit("/", 1)[1]
        self.calls.append((method, path, dict(params or {}), json))
        status, body = self.answers[(method, path)].pop(0)
        resp = type("R", (), {"status_code": status, "content": b"x", "text": str(body)})()
        resp.json = lambda: body
        return resp


def test_list_my_subscriptions_reads_every_page(monkeypatch):
    """Regression (F-089): the watchlist comes 50 subscriptions a page; stopping
    after the first page silently stopped watching everything else."""
    def page(ids, token=None):
        items = [{"snippet": {"title": f"T{i}", "resourceId": {"channelId": f"UC{i}"}}} for i in ids]
        return 200, dict(items=items, **({"nextPageToken": token} if token else {}))

    api = _YouTubeAPI(monkeypatch, {("GET", "subscriptions"): [
        page(range(50), "p2"), page(range(50, 100), "p3"), page([100])]})
    subs = youtube_playlist.list_my_subscriptions()
    assert [s["id"] for s in subs] == [f"UC{i}" for i in range(101)]
    assert subs[100] == {"id": "UC100", "title": "T100"}
    assert [c[2].get("pageToken") for c in api.calls] == [None, "p2", "p3"]


def test_add_video_recreates_a_deleted_playlist(monkeypatch, tmp_path):
    """Regression (F-089): a cached playlist deleted on YouTube answers 404;
    without recreating it every later Save failed."""
    state = tmp_path / "youtube_state.json"
    state.write_text('{"playlist_id": "PL_gone", "name": "Watchlist"}', encoding="utf-8")
    monkeypatch.setattr(config, "YOUTUBE_PLAYLIST_STATE_FILE", str(state))
    monkeypatch.setattr(config, "YOUTUBE_PLAYLIST_NAME", "Watchlist")
    gone = {"error": {"message": "Playlist not found", "errors": [{"reason": "playlistNotFound"}]}}
    api = _YouTubeAPI(monkeypatch, {
        ("GET", "playlistItems"): [(404, gone), (200, {"items": []})],
        ("GET", "playlists"): [(200, {"items": [{"id": "PL_other", "snippet": {"title": "Other"}}]})],
        ("POST", "playlists"): [(200, {"id": "PL_new"})],
        ("POST", "playlistItems"): [(200, {})],
    })
    assert youtube_playlist.add_video("dQw4w9WgXcQ") == "added"
    assert api.calls[-1][3]["snippet"] == {
        "playlistId": "PL_new", "resourceId": {"kind": "youtube#video", "videoId": "dQw4w9WgXcQ"}}
    assert youtube_playlist._read_playlist_id() == "PL_new"


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


def test_failed_sends_are_retried_next_check(offline, monkeypatch, archive_path, notifier):
    """Regression (F-077): no test ever made a send fail, so marking a video
    seen before sending it, marking unsent videos seen while no chat is known,
    or flagging the set-up message as sent before sending it all passed. Each
    loses a notification for good; checker.py promises a retry."""
    subs = _subs(1)
    monkeypatch.setattr(youtube_playlist, "list_my_subscriptions", lambda: subs)

    def fail_once(name):
        real, calls = getattr(notifier, name), []

        async def flaky(*args):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("chat unreachable")
            await real(*args)

        monkeypatch.setattr(notifier, name, flaky)

    fail_once("send_first_run")
    asyncio.run(checker.run_check(notifier, {}))   # set-up message fails
    asyncio.run(checker.run_check(notifier, {}))   # ...and goes out now
    assert notifier.sent == [("first_run", 1)]

    def feed_with_one_new(cid):
        title, vids = _feed_stub()(cid)
        vids.insert(0, {"video_id": "brand-new", "title": "n", "link": "l",
                        "published": FRESH, "author": "a"})
        return title, vids

    monkeypatch.setattr(discovery, "fetch_channel_feed", feed_with_one_new)
    asyncio.run(checker.run_check(_NoChat(), {}))  # found before /start: kept unseen
    fail_once("send_video")
    asyncio.run(checker.run_check(notifier, {}))   # the send fails
    asyncio.run(checker.run_check(notifier, {}))   # ...and is retried
    assert notifier.sent == [("first_run", 1), ("video", "brand-new")]

    def not_authorized():
        raise youtube_playlist.NotAuthorized("token dead")

    monkeypatch.setattr(youtube_playlist, "list_my_subscriptions", not_authorized)
    fail_once("send_auth_alert")
    state = {}
    asyncio.run(checker.run_check(notifier, state))  # the alert fails
    asyncio.run(checker.run_check(notifier, state))  # ...and is not throttled away
    assert notifier.sent[-1] == ("auth", "token dead")


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


def test_playlist_self_test_parses_its_arguments(monkeypatch, capsys):
    """Regression (F-078): every argument was taken as a video id, so `--help`
    (or a typo) ran the self-test and tried to insert "--help" twice."""
    def no_youtube():
        raise AssertionError("must not reach YouTube")

    monkeypatch.setattr(youtube_playlist, "_load_credentials", no_youtube)
    with pytest.raises(SystemExit) as exc:
        youtube_playlist.main(["--help"])
    assert exc.value.code == 0 and "video_id" in capsys.readouterr().out
    # The last two are 11 characters, but not ASCII (regression, F-078 reopened).
    for bad in (["--bogus"], ["not-an-id"], ["dQw4w9WgXcQ", "extra"],
                ["α" * 11], ["٣" * 11]):
        with pytest.raises(SystemExit) as exc:
            youtube_playlist.main(bad)
        assert exc.value.code == 2, bad
    assert youtube_playlist.parse_args(["dQw4w9WgXcQ"]).video_id == "dQw4w9WgXcQ"
    assert youtube_playlist.parse_args(["--", "-abcdefghij"]).video_id == "-abcdefghij"
    assert youtube_playlist.parse_args([]).video_id is None


def test_playlist_self_test_survives_a_console_without_emoji(monkeypatch, tmp_path):
    """Regression (F-075): with its output piped or redirected on Windows (the
    ANSI code page), the first "❌"/"🔗" line raised UnicodeEncodeError."""
    import io
    import sys

    pytest.importorskip("google.oauth2.credentials")
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(config, "YOUTUBE_TOKEN_FILE", str(tmp_path / "none.json"))
    assert youtube_playlist.main([]) == 1
    out.flush()
    assert "? YouTube isn't connected yet" in out.buffer.getvalue().decode("cp1252")


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


def test_shorts_and_stale_videos_are_recorded_not_returned(offline, monkeypatch, archive_path):
    """Regression (F-082): the fixture never produced a Short or an old
    video, so either filter could be dropped with every test passing."""
    subs = _subs(1)
    cid = subs[0]["id"]
    old = (datetime.now(timezone.utc) - timedelta(days=config.MAX_VIDEO_AGE_DAYS + 10)).isoformat()
    monkeypatch.setattr(youtube_playlist, "list_my_subscriptions", lambda: subs)

    def feed(channel_id):
        title, vids = _feed_stub()(channel_id)
        return title, vids + [
            {"video_id": vid, "title": vid, "link": "l", "published": published, "author": "a"}
            for vid, published in (("fresh", FRESH), ("short", FRESH), ("resurfaced", old))
        ]

    def classify(videos):
        for v in videos:
            v.update(type="video", is_short=v["video_id"] == "short")

    monkeypatch.setattr(discovery, "fetch_channel_feed", feed)
    monkeypatch.setattr(discovery, "classify_videos", classify)
    data = archive.load(str(archive_path))
    data["channels"] = [cid]  # already seeded; its 15 old uploads are new too
    videos, _, _ = discovery.collect_new_videos(data)
    returned = {v["video_id"] for v in videos}
    assert "fresh" in returned
    assert "short" not in returned and "resurfaced" not in returned
    assert "short" in data["seen"] and "resurfaced" in data["seen"]


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
