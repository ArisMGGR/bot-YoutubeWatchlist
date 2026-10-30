"""Persistent state for YtWatchlistBot.

The archive keeps track of:
  * channels  - list of channel IDs we have already initialised (seeded)
  * seen      - map of videoId -> {title, channel, channel_id} for videos we've
                notified on (or deliberately skipped: seeded, Shorts, stale)
  * resolved  - cache mapping an exceptions.txt input (handle/url) -> channel ID

Keeping `seen` lets us notify only on genuinely new videos, and `channels`
lets a newly subscribed channel be seeded silently instead of dumping its whole
back-catalogue into your chat.
"""
import json
import logging
import os
import threading

log = logging.getLogger("ytwatchlistbot.archive")

# Held around every read-modify-write of the archive file. A check keeps its
# own loaded copy for minutes while /start may persist the notify target from
# another thread; both sides take this lock so neither overwrites the other.
LOCK = threading.RLock()

# Keys written by /start rather than by the check. A check's save always takes
# these from the file on disk, never from its (possibly stale) snapshot.
NOTIFY_KEYS = ("notify_chat_id", "discord_channel_id", "discord_guild_id")


def load(path):
    data = {}
    if os.path.exists(path):
        # utf-8-sig: the README tells you to hand-edit this file, and a BOM
        # from a Windows editor would otherwise make it unreadable.
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
        except (ValueError, OSError) as exc:  # bad JSON, bad encoding, unreadable
            log.warning(
                "Couldn't read %s (%s) - starting from an empty archive; the "
                "next save replaces it.", path, exc,
            )
            data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("channels", [])
    data.setdefault("seen", {})
    data.setdefault("resolved", {})
    # Where to notify, learned from /start when the id isn't configured. Kept
    # per platform so switching between them doesn't inherit the wrong target.
    data.setdefault("notify_chat_id", None)        # Telegram chat
    data.setdefault("discord_channel_id", None)    # Discord channel
    data.setdefault("discord_guild_id", None)      # the server that channel is in
    # Whether the one-time "I'm set up" message has gone out. Archives written
    # before this flag existed already had their first check, so a non-empty
    # one counts as announced.
    data.setdefault("setup_announced", bool(data["channels"] or data["seen"]))
    return data


def set_keys(path, **values):
    """Update just these top-level keys in the archive file, atomically with
    respect to a check's save (see :data:`LOCK`)."""
    with LOCK:
        data = load(path)
        data.update(values)
        save(path, data)


def save_check(path, data):
    """Save a check's snapshot, keeping the notify target that is on disk now:
    /start may have registered one while the check was running."""
    with LOCK:
        current = load(path)
        for key in NOTIFY_KEYS:
            data[key] = current.get(key)
        save(path, data)


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def mark_seen(data, video, channel_title=None, channel_id=None):
    """Record `video` as seen. Every writer goes through here so each entry
    carries the channel id that :func:`prune` groups by."""
    data["seen"][video["video_id"]] = {
        "title": video.get("title"),
        "channel": channel_title if channel_title is not None else video.get("channel_title"),
        "channel_id": channel_id or video.get("channel_id"),
    }


def prune(data, keep_per_channel=50):
    """Keep the archive from growing unbounded.

    Entries are capped *per channel*: a channel's RSS feed only ever lists its
    15 most recent uploads, so keeping the newest `keep_per_channel` seen ids
    for each channel guarantees nothing still in a feed is forgotten — which is
    what a single global cap got wrong (with a few hundred subscriptions the
    seeded ids of the first channels were evicted and re-notified every check).
    Dicts preserve insertion order, so within a channel the oldest go first.
    Entries written before `channel_id` was recorded have no channel to group
    by; they're left alone (they're a bounded, one-off legacy set).
    """
    seen = data["seen"]
    by_channel = {}
    for key, meta in seen.items():
        cid = meta.get("channel_id") if isinstance(meta, dict) else None
        if cid:
            by_channel.setdefault(cid, []).append(key)
    for keys in by_channel.values():
        for key in keys[:-keep_per_channel] if len(keys) > keep_per_channel else ():
            del seen[key]
