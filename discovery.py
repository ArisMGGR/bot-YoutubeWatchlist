"""Channel discovery for YtWatchlistBot.

The watchlist is your YouTube account's **subscriptions** (read once per check
via OAuth). For each subscribed channel this reads its RSS feed, works out which
uploads are genuinely new, drops Shorts and stale re-surfaced videos, applies
your per-channel opt-outs from ``exceptions.txt``, and returns the rest ready to
notify. This module contains **no chat code** — `checker.py` does the sending
through whichever bot is running. Splitting it out keeps the
network-heavy work in one place that the bot can run off the event loop (via
``asyncio.to_thread``).
"""
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import archive as archive_mod
import config
import youtube_playlist
from youtube import (
    classify_videos,
    fetch_channel_feed,
    resolve_channel_id,
    short_feed_error,
)

log = logging.getLogger("ytwatchlistbot.discovery")

# --- Notification-type categories (used by the exceptions file) ------------- #
# classify_videos() labels each item "video", "live", "upcoming", or
# "livestream". For opt-outs we group them into two everyday buckets.
_VIDEO_TYPES = {"video"}
_LIVE_TYPES = {"live", "upcoming", "livestream"}

# Exception rules.
MUTE = "mute"              # no notifications from this channel at all
ONLY_VIDEOS = "videos"     # only regular uploads (mute livestreams/premieres)
ONLY_LIVE = "livestreams"  # only livestreams/premieres (mute regular uploads)


def is_stale(published, max_age_days):
    """True if `published` is older than `max_age_days` (0 disables the check).

    Guards against YouTube resurfacing an old video in the RSS feed. A missing
    or unparseable timestamp is treated as fresh, so a genuine new upload is
    never dropped on a parsing hiccup.
    """
    if max_age_days <= 0 or not published:
        return False
    try:
        dt = datetime.fromisoformat(published)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - dt > timedelta(days=max_age_days)


# Right-hand-side keyword synonyms -> canonical rule.
_RULE_KEYWORDS = {
    "": MUTE,
    "mute": MUTE,
    "none": MUTE,
    "off": MUTE,
    "video": ONLY_VIDEOS,
    "videos": ONLY_VIDEOS,
    "uploads": ONLY_VIDEOS,
    "live": ONLY_LIVE,
    "lives": ONLY_LIVE,
    "livestream": ONLY_LIVE,
    "livestreams": ONLY_LIVE,
    "stream": ONLY_LIVE,
    "streams": ONLY_LIVE,
}

# A URL's `?query` part (up to the next space), e.g. a share link's `?si=...`.
_URL_QUERY_RE = re.compile(r"\?\S*")


def parse_exceptions_file(path):
    """Read per-channel notification opt-outs.

    Each non-comment line is ``<channel>`` or ``<channel> = <rule>``:

      * ``<channel>``  an @handle, a channel URL, or a ``UC...`` id
      * ``<rule>``     omitted        -> mute the channel entirely
                       ``videos``     -> only regular videos (mute livestreams)
                       ``livestreams``-> only livestreams/premieres (mute videos)

    Blank lines and ``#`` comments are ignored. Returns a list of
    ``(channel_input, rule)`` tuples where ``rule`` is one of MUTE, ONLY_VIDEOS,
    or ONLY_LIVE. An unrecognized rule is logged and the channel is muted (the
    safe default for an opt-out file).
    """
    if not path or not os.path.exists(path):
        return []
    rules = []
    # utf-8-sig: a BOM from a Windows editor would otherwise glue itself to the
    # first line's channel and make it unresolvable (config.py does the same).
    with open(path, encoding="utf-8-sig") as f:
        for raw in f:
            # Strip inline (and whole-line) `#` comments. Channel refs never
            # contain `#`, so this lets bot-written lines carry a `# Name` label.
            # Drop URL query strings too: YouTube's share links end in
            # `?si=...`, whose `=` would otherwise be read as the rule's.
            line = _URL_QUERY_RE.sub("", raw.split("#", 1)[0]).strip()
            if not line:
                continue
            channel, sep, rhs = line.partition("=")
            channel = channel.strip()
            if not channel:
                continue
            keyword = rhs.strip().lower()
            rule = _RULE_KEYWORDS.get(keyword)
            if rule is None:
                log.warning(
                    "exceptions.txt: unknown rule %r for %s — muting the channel.",
                    rhs.strip(), channel,
                )
                rule = MUTE
            rules.append((channel, rule))
    return rules


def _resolve_exceptions(rules, resolved):
    """Map ``(channel_input, rule)`` pairs to a ``{channel_id: rule}`` dict.

    Handles/URLs are resolved to canonical ``UC...`` ids (cached in `resolved`,
    the same cache used for the watchlist) so they can be matched against the
    ids returned by the subscriptions API.
    """
    out = {}
    for inp, rule in rules:
        cid = resolved.get(inp) or resolve_channel_id(inp)
        if not cid:
            log.warning("Could not resolve exception channel: %s", inp)
            continue
        resolved[inp] = cid
        out[cid] = rule
    return out


def _rule_allows(rule, vtype):
    """True if a video of `vtype` should still be notified under `rule`."""
    if rule == ONLY_VIDEOS:
        return vtype in _VIDEO_TYPES
    if rule == ONLY_LIVE:
        return vtype in _LIVE_TYPES
    return True  # no rule (MUTE channels are skipped before we get here)


# --------------------------------------------------------------------------- #
# Looking up / editing exceptions from the bot (the /except command)
# --------------------------------------------------------------------------- #
_REF_RE = re.compile(r"UC[\w-]{22}")


def find_subscriptions(text, subs):
    """Return the subscriptions matching `text`, best match first.

    Matching order: exact title (case-insensitive); then an @handle / channel
    URL / ``UC...`` id resolved to a channel you're subscribed to; then a title
    substring. `subs` is the list from :func:`youtube_playlist.list_my_subscriptions`.
    May do network I/O (resolving a handle) — call from a worker thread.
    """
    t = text.strip()
    if not t:
        return []  # "" is a substring of every title
    tl = t.lower()

    exact = [s for s in subs if (s.get("title") or "").lower() == tl]
    if exact:
        return exact

    # Looks like a channel reference? Resolve it and match by id.
    if t.startswith("http") or t.startswith("@") or _REF_RE.fullmatch(t):
        cid = resolve_channel_id(t)
        if cid:
            return [s for s in subs if s["id"] == cid]

    return [s for s in subs if tl in (s.get("title") or "").lower()]


def lookup_subscription(text):
    """Fetch subscriptions and return those matching `text` (see
    :func:`find_subscriptions`).

    Blocking — call from a worker thread. Raises
    :class:`youtube_playlist.NotAuthorized` if YouTube isn't connected.
    """
    return find_subscriptions(text, youtube_playlist.list_my_subscriptions())


def set_exception(path, channel_id, rule, title=None):
    """Add or update the opt-out rule for `channel_id` in the exceptions file.

    The channel id is stored (so discovery matches it exactly), with the title
    kept as a trailing ``# comment`` for readability. An existing line for the
    same id is replaced in place; comments and other rules are preserved.
    Returns ``"added"`` or ``"updated"``.
    """
    comment = f"  # {title}" if title else ""
    if rule == MUTE:
        new_line = f"{channel_id}{comment}"
    else:
        new_line = f"{channel_id} = {rule}{comment}"

    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()

    for i, raw in enumerate(lines):
        stripped = raw.split("#", 1)[0].strip()
        if not stripped:
            continue
        token = stripped.partition("=")[0].strip()
        if token.lower() == channel_id.lower():
            lines[i] = new_line
            outcome = "updated"
            break
    else:
        lines.append(new_line)
        outcome = "added"

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return outcome


def collect_new_videos(data):
    """Update `data` in place and return ``(videos, seeded_channels, watched)``.

    `videos` are fresh, classified, non-Short new uploads, sorted oldest-first
    and ready to send. `watched` is the number of channels whose feed was read
    this pass. It is None when nothing could be read — the subscriptions, or
    every feed there was to fetch (the failure is logged here) — and 0 only
    when there was genuinely nothing to watch (no subscriptions, or all of
    them muted). Seeding a newly added channel, and recording Shorts /
    stale videos as seen, all happen here (they're never notified). Marking the
    returned videos as seen is deliberately left to the caller, so an item is
    recorded only once its notification has actually gone out.

    This does blocking network I/O — call it from a worker thread. Raises
    :class:`youtube_playlist.NotAuthorized` if YouTube isn't connected.
    """
    resolved = data["resolved"]

    # The watchlist: this account's YouTube subscriptions (needs OAuth). If the
    # token is missing or dead there's nothing to check until
    # `manual-setup/authorize_youtube.py` is re-run, so NotAuthorized is raised to the caller
    # (which alerts you in the chat) rather than swallowed like a transient error.
    try:
        subscriptions = youtube_playlist.list_my_subscriptions()
    except youtube_playlist.NotAuthorized:
        raise
    except Exception as exc:  # noqa: BLE001 - a transient API error shouldn't crash the loop
        log.warning("Failed to read your subscriptions: %s", exc)
        return [], [], None

    # Per-channel opt-outs. Muted channels are skipped entirely; type rules are
    # applied after classification below.
    exceptions = _resolve_exceptions(
        parse_exceptions_file(config.EXCEPTIONS_FILE), resolved
    )

    new_videos = []
    seeded_channels = []
    watched = 0
    fetched_any = False
    attempted = 0
    # Recognized feed errors are collected here ({short label: [channel names]})
    # and logged as one grouped summary after the pass, instead of one long
    # WARNING per channel. Unrecognized errors are still logged individually.
    skipped = {}

    for sub in subscriptions:
        cid = sub["id"]
        if exceptions.get(cid) == MUTE:
            continue  # fully opted out — don't even fetch its feed

        # Pace the feed requests: pausing between fetches keeps the pass under
        # YouTube's burst throttle, which is what triggers the spurious 404/5xx
        # in the first place (see config.FEED_FETCH_PACING_SECONDS). Only pause
        # *between* fetches, so the first channel isn't delayed.
        if fetched_any and config.FEED_FETCH_PACING_SECONDS > 0:
            time.sleep(config.FEED_FETCH_PACING_SECONDS)
        fetched_any = True

        attempted += 1
        try:
            channel_title, videos = fetch_channel_feed(cid)
        except Exception as exc:  # noqa: BLE001 - keep going on a single failure
            name = sub.get("title") or cid
            label = short_feed_error(exc)
            if label:
                # Recognized error: collect it for the grouped summary below
                # instead of logging a long line per channel.
                skipped.setdefault(label, []).append(name)
            else:
                # Unrecognized error: keep the raw detail so it's still debuggable.
                log.warning("Failed to fetch feed for %s (%s): %s", name, cid, exc)
            continue

        channel_title = channel_title or sub.get("title") or cid
        watched += 1

        if cid not in data["channels"]:
            # Newly subscribed (or first-ever) channel: seed silently so we
            # don't spam the whole back-catalogue.
            for v in videos:
                archive_mod.mark_seen(data, v, channel_title, cid)
            data["channels"].append(cid)
            seeded_channels.append(channel_title)
            continue

        for v in videos:
            if v["video_id"] not in data["seen"]:
                v["channel_title"] = channel_title
                v["channel_id"] = cid
                new_videos.append(v)

    # One grouped summary for all the recognized feed errors this pass, instead
    # of a long WARNING per channel. Groups are ordered most-common first, each
    # listing how many channels hit that error.
    if skipped:
        total = sum(len(names) for names in skipped.values())
        lines = [f"Skipped {total} of {attempted} channel(s) this check:"]
        for label, names in sorted(skipped.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            lines.append(f"  • {label} ×{len(names)}")
        log.warning("\n".join(lines))

    # Forget channels you've unsubscribed from or muted, so if they come back
    # they're seeded silently again instead of dumping everything they posted
    # in the meantime. (A channel whose feed merely failed this pass is still
    # subscribed and unmuted, so it stays.)
    active = {s["id"] for s in subscriptions if exceptions.get(s["id"]) != MUTE}
    data["channels"] = [c for c in data["channels"] if c in active]

    # Drop stale entries before any per-video lookups; record them as seen so
    # they aren't re-checked.
    fresh = []
    for v in new_videos:
        if is_stale(v.get("published"), config.MAX_VIDEO_AGE_DAYS):
            archive_mod.mark_seen(data, v)
        else:
            fresh.append(v)
    new_videos = fresh

    # Classify (video / live / upcoming / livestream), then drop Shorts and any
    # types the channel is opted out of in exceptions.txt. Dropped items are
    # recorded as seen so they aren't reconsidered on the next check.
    classify_videos(new_videos)
    keep = []
    for v in new_videos:
        rule = exceptions.get(v.get("channel_id"))
        if v.get("is_short") or not _rule_allows(rule, v.get("type", "video")):
            archive_mod.mark_seen(data, v)
        else:
            keep.append(v)
    new_videos = keep

    # Oldest-first so notifications arrive in chronological order.
    new_videos.sort(key=lambda v: v.get("published") or "")
    if attempted and not watched:
        return new_videos, seeded_channels, None  # every feed failed
    return new_videos, seeded_channels, watched
