"""Daily log files for YtWatchlistBot.

Everything the bot already logs — new videos, notifications, errors, check
summaries — is also written to a plain-text file under ``logs/``. All runs on
the same calendar day append to the *same* file (``logs/2026-08-11.log``); when
the day rolls over, the next log line lands in a fresh file for the new day.

Console logging (set up by ``logging.basicConfig`` in the bot) is untouched —
this just adds a second destination on the root logger, so every module's logs
are captured without changing any call sites.
"""
import logging
import os
from datetime import date

import config


class DailyFileHandler(logging.Handler):
    """Write each record to ``logs/<YYYY-MM-DD>.log``, switching file at midnight.

    Unlike ``TimedRotatingFileHandler``, the *active* file is named by its date,
    so several runs in one day share one file and there's never a base file to
    rename. The date is re-checked on every record, so a long-running process
    started before midnight starts a new file on its own once the day changes.
    """

    def __init__(self, log_dir):
        super().__init__()
        self.log_dir = log_dir
        self._current_day = None
        self._stream = None
        os.makedirs(self.log_dir, exist_ok=True)

    def _path_for(self, day):
        return os.path.join(self.log_dir, f"{day.isoformat()}.log")

    def _ensure_stream(self):
        today = date.today()
        if today != self._current_day:
            if self._stream is not None:
                self._stream.close()
            self._stream = open(self._path_for(today), "a", encoding="utf-8")
            self._current_day = today
        return self._stream

    def emit(self, record):
        try:
            stream = self._ensure_stream()
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:  # noqa: BLE001 - logging must never crash the bot
            self.handleError(record)

    def close(self):
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        finally:
            super().close()


def setup_file_logging(level=logging.INFO):
    """Attach a daily file handler to the root logger. Returns the handler.

    Safe to call once at startup, after ``logging.basicConfig``. Calling it more
    than once is a no-op (it won't add a second file handler).
    """
    root = logging.getLogger()
    for existing in root.handlers:
        if isinstance(existing, DailyFileHandler):
            return existing

    handler = DailyFileHandler(config.LOG_DIR)
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s  %(levelname)-8s %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    return handler
