"""The periodic channel check, independent of any chat platform.

The bot can talk to you on Telegram or on Discord. Everything *around* the
messages is identical either way — read the subscriptions, work out what's new,
send it, remember what was sent — so that logic lives here and each platform
supplies a small :class:`Notifier` that knows how to phrase and post a message.

A notifier must provide:

    chat_known                 -> bool   (do we know where to post yet?)
    async send_video(video)             (one new video/livestream)
    async send_first_run(watched)       ("I'm set up, watching N channels")
    async send_seeded(names)            ("now watching these new channels")
    async send_auth_alert(reason)       ("re-run manual-setup/authorize_youtube.py")
    describe_error(exc)        -> str   (friendly one-liner, or "")

Any of the send methods may raise; the check survives it and retries next pass.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

import archive as archive_mod
import config
import discovery
import youtube_playlist

log = logging.getLogger("ytwatchlistbot.check")

# type -> (emoji, human label). Shared so both bots label things identically.
TYPE_META = {
    "video": ("▶️", "New video"),
    "live": ("🔴", "LIVE now"),
    "upcoming": ("🗓", "Scheduled livestream"),
    "livestream": ("📺", "New livestream"),
}


def fmt_published(published):
    """``2026-07-20T18:00:00+00:00`` -> ``2026-07-20 18:00 UTC`` (raw on error)."""
    if not published:
        return None
    try:
        dt = datetime.fromisoformat(published)
    except ValueError:
        return published
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# "Now watching" names at most this many channels, in at most this many
# (escaped) characters; the rest are counted. Many channels can be seeded at
# once (e.g. after /authorize with another account), and an unbounded list
# overruns Discord's 2000 / Telegram's 4096-character message limit.
SEEDED_LIST_MAX = 15
SEEDED_LIST_CHARS = 1500


def seeded_listing(names, escape):
    """The bullet list for a "now watching" message, capped as above."""
    lines, used = [], 0
    for i, name in enumerate(names):
        line = f"• {escape(name)}"
        if i >= SEEDED_LIST_MAX or used + len(line) > SEEDED_LIST_CHARS:
            lines.append(f"…and {len(names) - i} more")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


# Only one check runs at a time (the periodic loop and a manual /check must not
# stomp on each other's archive writes). Created inside the running loop, not
# at import: on Python 3.9 a Lock binds the loop current at creation, and
# discord.py runs its client in a new loop, so an import-time lock failed with
# "attached to a different loop" whenever a check had to wait for it.
_check_lock = None
_check_lock_loop = None


def check_lock():
    """The check lock for the running event loop."""
    global _check_lock, _check_lock_loop
    loop = asyncio.get_running_loop()
    if _check_lock is None or _check_lock_loop is not loop:
        _check_lock, _check_lock_loop = asyncio.Lock(), loop
    return _check_lock

# A dead YouTube token stops every check, so it's worth a message — but not one
# per check. Repeat the alert at most this often (seconds).
AUTH_ALERT_REPEAT_SECONDS = 6 * 3600


def _prune_and_save(data):
    archive_mod.prune(data)
    archive_mod.save_check(config.ARCHIVE_FILE, data)


async def _alert_not_authorized(notifier, state, exc, *, force=False):
    """Tell the owner that YouTube needs re-authorizing.

    Checks are dead in the water until `manual-setup/authorize_youtube.py` runs again, so
    this mustn't stay buried in the log. Repeats are throttled to one message
    per AUTH_ALERT_REPEAT_SECONDS (a broken token would otherwise ping the chat
    every check); a manual check always gets an answer.
    """
    log.warning("Can't read your subscriptions - YouTube isn't connected: %s", exc)
    if not notifier.chat_known:
        return
    now = time.monotonic()
    last = state.get("auth_alert_at")
    if not force and last is not None and now - last < AUTH_ALERT_REPEAT_SECONDS:
        return
    try:
        await notifier.send_auth_alert(str(exc))
    except Exception as send_exc:  # noqa: BLE001 - alert failures must not crash the loop
        log.warning(
            "Couldn't send the re-authorize alert: %s",
            notifier.describe_error(send_exc) or send_exc,
        )
        return
    state["auth_alert_at"] = now


async def run_check(notifier, state, *, trigger="scheduled"):
    """Check every channel once and notify about anything new.

    `state` is a plain dict the caller keeps between runs (alert throttling).

    Network-heavy discovery runs in a worker thread so button taps stay
    responsive. A video is marked seen only after its message is sent, so a
    failed send is simply retried on the next check.
    """
    async with check_lock():
        data = await asyncio.to_thread(archive_mod.load, config.ARCHIVE_FILE)
        # Tracked explicitly, not inferred from an empty archive: a check that
        # runs before /start seeds the channels but can't announce anything.
        first_run = not data["setup_announced"]

        try:
            videos, seeded, watched = await asyncio.to_thread(
                discovery.collect_new_videos, data
            )
        except youtube_playlist.NotAuthorized as exc:
            await _alert_not_authorized(
                notifier, state, exc, force=(trigger == "manual")
            )
            return
        # Reading subscriptions worked, so a later break alerts again straight away.
        state.pop("auth_alert_at", None)

        if not notifier.chat_known:
            if videos or seeded:
                log.warning(
                    "%d new video(s) found but no chat to notify yet - send "
                    "/start to your bot so I know where to message you.",
                    len(videos),
                )
            # Persist seeding / resolved IDs, but leave the unsent videos unseen
            # so they go out once a chat is registered.
            await asyncio.to_thread(_prune_and_save, data)
            return

        sent = 0
        for v in videos:
            try:
                await notifier.send_video(v)
            except Exception as exc:  # noqa: BLE001 - one bad send mustn't stop the rest
                friendly = notifier.describe_error(exc)
                log.warning("Couldn't send %s: %s", v["video_id"], friendly or exc)
                continue  # leave it unseen -> retried next check
            archive_mod.mark_seen(data, v)
            sent += 1
            log.info(
                "New %s notified: %s - %s (%s)",
                v.get("type", "video"),
                v.get("channel_title") or v.get("author") or "?",
                v.get("title") or "?",
                v["video_id"],
            )
            await asyncio.sleep(0.5)  # stay under the platform's rate limit

        # Friendly status messages. The "set up" message is sent once, on the
        # first check that actually read something: with watched None the
        # subscriptions (or every feed) couldn't be read, and the message would
        # repeat on every check until it works. watched == 0 is a real answer
        # (no subscriptions, or all muted) and still gets the message.
        try:
            if first_run and watched is None:
                log.warning(
                    "First check couldn't read any channel yet (see the warnings "
                    "above) - will try again next check."
                )
            elif first_run:
                await notifier.send_first_run(watched)
                data["setup_announced"] = True
            elif seeded:
                await notifier.send_seeded(seeded)
        except Exception as exc:  # noqa: BLE001 - a status message is never critical
            log.warning(
                "Couldn't send status message: %s", notifier.describe_error(exc) or exc
            )

        await asyncio.to_thread(_prune_and_save, data)
        log.info(
            "Check (%s) done: watched %d channel(s), sent %d, seeded %d.",
            trigger, watched or 0, sent, len(seeded),
        )


async def check_loop(notifier, state):
    """Runs forever: check channels, then wait CHECK_INTERVAL_MINUTES, repeat.

    The first check runs shortly after startup so a fresh install seeds (and a
    restart catches up) right away.
    """
    interval = max(1, config.CHECK_INTERVAL_MINUTES) * 60
    await asyncio.sleep(5)  # let the connection settle first
    while True:
        try:
            await run_check(notifier, state)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive no matter what
            friendly = notifier.describe_error(exc)
            if friendly:
                log.warning("During channel check: %s", friendly)
            else:
                log.error("Unexpected error during channel check:", exc_info=exc)
        await asyncio.sleep(interval)
