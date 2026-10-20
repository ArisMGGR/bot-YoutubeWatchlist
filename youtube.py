"""YouTube helpers: resolve channels, read RSS feeds, classify new videos."""
import logging
import random
import re
import time
import xml.etree.ElementTree as ET

import requests

import config

log = logging.getLogger("ytwatchlistbot.youtube")

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
API_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
SHORTS_URL = "https://www.youtube.com/shorts/{vid}"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

_CHANNEL_ID_RE = re.compile(r"UC[\w-]{22}")

session = requests.Session()
session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
)
# Skip YouTube's cookie-consent interstitial (which contains no channel ID).
session.cookies.set("SOCS", "CAI", domain=".youtube.com")
session.cookies.set("CONSENT", "YES+cb", domain=".youtube.com")

# Status codes that mean "try again in a moment", not "this is gone". YouTube's
# feed edge throttles bursts of requests by returning 500/503 — and, confusingly,
# spurious 404s — even for channels that exist and load fine seconds later. (A
# genuinely deleted channel returns 404 *consistently*; a throttled one flaps
# between 404 and 500 from one check to the next.) Retrying these with backoff
# recovers the channel within the same check instead of dropping it.
_RETRY_STATUS = frozenset({404, 429, 500, 502, 503, 504})


def _get_with_retries(url, **kwargs):
    """``session.get`` that retries transient failures with backoff + jitter.

    Retries on connection/timeout errors and on the transient HTTP statuses in
    ``_RETRY_STATUS`` (see config.FEED_FETCH_RETRIES / FEED_FETCH_BACKOFF).
    The final attempt's response (or exception) is returned/raised unchanged, so
    the caller's ``raise_for_status()`` still surfaces a real, persistent error.
    """
    attempts = max(1, config.FEED_FETCH_RETRIES + 1)  # always at least one try
    last_exc = None
    for attempt in range(attempts):
        try:
            resp = session.get(url, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            resp = None
        else:
            last_exc = None
            if resp.status_code not in _RETRY_STATUS:
                return resp

        if attempt == attempts - 1:
            break  # out of retries — hand the last result back to the caller
        # Exponential backoff (base, 2x, 4x, ...) with jitter so a batch of
        # channels throttled together doesn't retry in lockstep.
        delay = config.FEED_FETCH_BACKOFF * (2 ** attempt)
        delay += random.uniform(0, config.FEED_FETCH_BACKOFF)
        reason = last_exc if last_exc is not None else f"HTTP {resp.status_code}"
        log.debug("Retrying %s in %.1fs (%s)", url, delay, reason)
        time.sleep(delay)

    if last_exc is not None:
        raise last_exc
    return resp


# --------------------------------------------------------------------------- #
# Channel resolution
# --------------------------------------------------------------------------- #
def resolve_channel_id(entry):
    """Turn an exceptions.txt channel reference into a canonical UC... channel ID.

    Accepts a raw channel ID, a channel/handle URL, or an @handle. Handles and
    custom URLs are resolved by fetching the channel page and extracting the ID.
    Returns None if it can't be resolved.
    """
    entry = entry.strip()
    if not entry:
        return None

    # Already a bare channel ID.
    if _CHANNEL_ID_RE.fullmatch(entry):
        return entry

    # A /channel/UC... URL.
    m = re.search(r"channel/(UC[\w-]{22})", entry)
    if m:
        return m.group(1)

    # A handle, custom URL, or /user/ URL -> fetch the page and scrape the ID.
    if entry.startswith("http"):
        url = entry
    else:
        handle = entry if entry.startswith("@") else "@" + entry.lstrip("@")
        url = "https://www.youtube.com/" + handle

    try:
        resp = session.get(url, params={"hl": "en"}, timeout=30)
    except requests.RequestException:
        return None

    if resp.status_code == 404:
        log.warning("Channel not found (404): %s — check the handle/URL.", entry)
        return None

    html_text = resp.text
    # Order matters: the first group of patterns identify the page's OWN
    # channel unambiguously. The "channelId" JSON key and a bare /channel/ link
    # appear many times on a page (featured/related channels, video owners), so
    # they must come last to avoid resolving to the wrong channel.
    for pattern in (
        r'rel="canonical"[^>]+href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
        r'property="og:url"[^>]+content="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
        r'<meta[^>]+itemprop="(?:identifier|channelId)"[^>]+content="(UC[\w-]{22})"',
        r'"externalId":"(UC[\w-]{22})"',
        r'"channelId":"(UC[\w-]{22})"',
        r'youtube\.com/channel/(UC[\w-]{22})',
    ):
        m = re.search(pattern, html_text)
        if m:
            return m.group(1)
    return None


# --------------------------------------------------------------------------- #
# Feed reading
# --------------------------------------------------------------------------- #
def fetch_channel_feed(channel_id):
    """Return (channel_title, [video, ...]) for a channel's RSS feed.

    Each video is a dict: video_id, title, link, published, author.
    """
    resp = _get_with_retries(RSS_URL.format(cid=channel_id), timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    channel_title = root.findtext("atom:title", default="", namespaces=NS)
    videos = []
    for entry in root.findall("atom:entry", NS):
        vid = entry.findtext("yt:videoId", namespaces=NS)
        if not vid:
            continue
        link_el = entry.find("atom:link", NS)
        link = (
            link_el.get("href")
            if link_el is not None
            else f"https://youtu.be/{vid}"
        )
        videos.append(
            {
                "video_id": vid,
                "title": entry.findtext("atom:title", default="", namespaces=NS),
                "link": link,
                "published": entry.findtext(
                    "atom:published", default="", namespaces=NS
                ),
                "author": entry.findtext(
                    "atom:author/atom:name", default="", namespaces=NS
                ),
            }
        )
    return channel_title, videos


def short_feed_error(exc):
    """A short label for a feed error, e.g. ``"not found (404)"``.

    A compact tag suitable for grouping many per-channel skips into one summary
    line. Nearly all of these are transient network or YouTube-side blips (see
    ``_RETRY_STATUS`` above): the channel is simply skipped for this one check
    and retried on the next. Returns ``""`` for an error we don't recognize, so
    the caller keeps logging those individually with their raw detail.
    """
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "couldn't reach YouTube"
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(exc.response, "status_code", None)
        if status == 404:
            return "not found (404)"
        if status == 429:
            return "rate-limited (429)"
        if status in (500, 502, 503, 504):
            return f"server error ({status})"
        if status is not None:
            return f"HTTP {status}"
        return "HTTP error"
    if isinstance(exc, ET.ParseError):
        return "malformed feed"
    if isinstance(exc, requests.exceptions.RequestException):
        return "network problem"
    return ""  # unknown — caller keeps the raw error text on its own line


# --------------------------------------------------------------------------- #
# Video classification (video vs livestream vs upcoming)
# --------------------------------------------------------------------------- #
def classify_videos(videos):
    """Annotate each video with a `type` (and `scheduled_start` if known).

    Uses the YouTube Data API when a key is configured (accurate + gives the
    scheduled start time), otherwise falls back to scraping each watch page.
    Types: "video", "live", "upcoming", "livestream".
    """
    if not videos:
        return

    if config.YOUTUBE_API_KEY:
        info = _classify_via_api([v["video_id"] for v in videos])
        for v in videos:
            meta = info.get(v["video_id"], {})
            v["type"] = meta.get("type", "video")
            v["scheduled_start"] = meta.get("scheduled_start")
    else:
        for v in videos:
            v["type"] = _classify_via_scrape(v["video_id"])
            v.setdefault("scheduled_start", None)

    # Flag Shorts so callers can drop them. Only plain "video" entries can be a
    # Short — live / upcoming / ended-livestream entries never are, so skip the
    # extra request for those.
    for v in videos:
        v["is_short"] = v["type"] == "video" and is_short(v["video_id"])


def is_short(video_id):
    """Return True if `video_id` is a YouTube Short.

    YouTube serves a real Short at its ``/shorts/<id>`` URL (HTTP 200) but
    redirects a regular video away from that path to the normal watch page. We
    send a lightweight HEAD request and inspect the status: a 200 means Short,
    a redirect means regular video. On any network error we default to False —
    better to let a genuine video through than to silently drop it.
    """
    try:
        resp = session.head(
            SHORTS_URL.format(vid=video_id),
            allow_redirects=False,
            timeout=30,
        )
    except requests.RequestException:
        return False
    return resp.status_code == 200


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _classify_via_api(video_ids):
    result = {}
    for chunk in _chunks(video_ids, 50):
        params = {
            "part": "snippet,liveStreamingDetails",
            "id": ",".join(chunk),
            "key": config.YOUTUBE_API_KEY,
        }
        data = None
        try:
            resp = session.get(API_VIDEOS_URL, params=params, timeout=30)
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            # Only the exception type: a requests error's text carries the full
            # request URL, and with it the API key.
            log.warning(
                "YouTube API lookup failed (%s) - falling back to scraping.",
                type(exc).__name__,
            )
        else:
            # An invalid key, exhausted quota or disabled API comes back as an
            # error object with no "items"; reading that as "no items" would
            # label every live stream a regular video.
            if not resp.ok or not isinstance(data, dict) or "error" in data:
                err = data.get("error", {}) if isinstance(data, dict) else {}
                log.warning(
                    "YouTube API returned HTTP %s (%s) - falling back to scraping.",
                    resp.status_code,
                    (err.get("message") if isinstance(err, dict) else err) or "no detail",
                )
                data = None
        if data is None:
            for vid in chunk:
                result[vid] = {"type": _classify_via_scrape(vid), "scheduled_start": None}
            continue
        for item in data.get("items", []):
            live = item.get("snippet", {}).get("liveBroadcastContent", "none")
            lsd = item.get("liveStreamingDetails") or {}
            if live == "live":
                vtype = "live"
            elif live == "upcoming":
                vtype = "upcoming"
            elif lsd:
                vtype = "livestream"  # a stream that has already ended
            else:
                vtype = "video"
            result[item["id"]] = {
                "type": vtype,
                "scheduled_start": _fmt_time(lsd.get("scheduledStartTime")),
            }
    return result


def _classify_via_scrape(video_id):
    try:
        html_text = session.get(
            f"https://www.youtube.com/watch?v={video_id}", timeout=30
        ).text
    except requests.RequestException:
        return "video"
    if '"isLiveContent":true' not in html_text:
        return "video"
    if '"isLiveNow":true' in html_text or '"isLive":true' in html_text:
        return "live"
    if '"isUpcoming":true' in html_text or '"scheduledStartTime"' in html_text:
        return "upcoming"
    return "livestream"


def _fmt_time(iso):
    if not iso:
        return None
    # 2026-07-20T18:00:00Z -> 2026-07-20 18:00 UTC
    return iso.replace("T", " ").replace("Z", "").rsplit(":", 1)[0] + " UTC"
