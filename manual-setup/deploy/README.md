# Running YtWatchlistBot as a systemd service

The bot is a **single always-on process** (`ytwatchlistbot.py`): a background
loop checks your channels on a timer and sends notifications, and the
same process handles the 💾 Save / 🗑 Delete buttons and chat commands. There's
no cron and no second process to run. All state (`archive.json`,
`youtube_state.json`) stays on the server next to the code.

This folder installs that process as a systemd service so it survives reboots.
Set the bot up first — `python3 manual-setup/setup_bot.py`, or the manual
steps in the main [README](../../README.md) — and check that
`python3 ytwatchlistbot.py` runs and answers `/start` before installing it.
(`setup_bot.py` offers to run the installer below for you at the end.)

## Install

### Easiest: the auto-installer (no prompts)

Run this **with `sudo` from your normal login** — root is needed to write
`/etc/systemd/system`, but the service's `User=` is taken from the account that
invoked `sudo`:

```bash
sudo bash manual-setup/deploy/install-service.sh
```

It detects everything and fills in the template for you:

| Field | Detected from |
|-------|---------------|
| `User=` | `$SUDO_USER` (the account that ran sudo; falls back to `whoami`) |
| `WorkingDirectory=` | this repo's root |
| `ExecStart` python | `venv/bin/python3` if present, else system `python3` |
| `EnvironmentFile=` | `<repo>/conf.env` |

then reloads systemd, enables the service, starts it, watches it for a few
seconds, and reports whether it stayed up (a bot that exits on a config error
is reported with its recent logs). Preview without changing anything using
`--dry-run`; override a value without a prompt with `SERVICE_USER=you`,
`PYTHON=/path/to/python3` or `SETTLE_SECONDS=15` (how long it watches).

Run the scripts with `bash` as shown: they work whether or not the files
carry an executable bit (a checkout made from Windows may not).

> It warns (but still installs) if it finds no bot token — no `conf.env`,
> `token.txt` or `discord_token.txt` — since the bot would then crash-loop.

### Manual alternative

`youtube-watchlist-bot.service.example` in this folder is a template. **Copy
it first** (the copy is gitignored, so your edits stay local and never block a
`git switch`/`pull` or get pushed), then edit the three marked lines — `User`,
`WorkingDirectory`, and the `python3` path (`which python3`):

```bash
cp manual-setup/deploy/youtube-watchlist-bot.service.example manual-setup/deploy/youtube-watchlist-bot.service
nano manual-setup/deploy/youtube-watchlist-bot.service   # set User / WorkingDirectory / python3
```

Then install your copy:

```bash
sudo cp manual-setup/deploy/youtube-watchlist-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now youtube-watchlist-bot.service
```

Check on it:

```bash
systemctl status youtube-watchlist-bot.service
journalctl -u youtube-watchlist-bot.service -f     # watch checks + taps live
```

## Uninstall

To stop and remove the service, run the helper script:

```bash
sudo bash manual-setup/deploy/uninstall-service.sh          # asks to confirm
sudo bash manual-setup/deploy/uninstall-service.sh --yes    # no prompt
```

It stops the service, disables autostart, and deletes the unit file from
`/etc/systemd/system/` (including a leftover unit from the old
`ytwatchlistbot.service` name, if present). Your project files, `conf.env`, and
state (`archive.json` / `youtube_state.json`) are left untouched.

## Notes

- Run only **one** instance per bot token. Two processes polling the same bot
  get a Telegram "Conflict" error (the bot logs a clear message if this
  happens).
- `Restart=always` means it comes back after a crash or reboot.
- Change how often it checks with `CHECK_INTERVAL_MINUTES` in `conf.env`
  (default 30).
