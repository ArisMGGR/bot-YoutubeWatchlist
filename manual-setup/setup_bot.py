#!/usr/bin/env python3
"""One-shot setup for YtWatchlistBot: venv, dependencies, conf.env, OAuth.

Run it with any Python 3.9+ — it only uses the standard library, because it has
to work *before* the virtualenv exists:

    python manual-setup/setup_bot.py            (Windows)
    python3 manual-setup/setup_bot.py           (Linux / macOS)

What it does, in order:
  1. checks you're in the project folder and your Python is new enough;
  2. asks which chat platform you want (Telegram or Discord);
  3. creates `venv/` and installs the dependencies for that platform;
  4. walks you through every setting and writes `conf.env`;
  5. verifies the bot token against Telegram/Discord and tells you the bot's name;
  6. offers to run the one-time `manual-setup/authorize_youtube.py` YouTube connection;
  7. on Linux, offers to install the systemd service so it starts on boot.

Safe to re-run: an existing `venv/` is reused (dependencies are just refreshed),
and an existing `conf.env` is shown to you and left alone unless you say
otherwise — and even then the old one is kept as `conf.env.bak`.

The venv lives in `venv/` on purpose: `manual-setup/deploy/install-service.sh` looks for
`venv/bin/python3` and will point the service at it automatically.
"""
import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# This script lives in manual-setup/; everything it creates or checks belongs
# in the project folder one level up.
SETUP_DIR = Path(__file__).resolve().parent
HERE = SETUP_DIR.parent
AUTHORIZE = SETUP_DIR / "authorize_youtube.py"
VENV_DIR = HERE / "venv"
CONF = HERE / "conf.env"
REQUIREMENTS = HERE / "requirements.txt"
EXCEPTIONS_EXAMPLE = SETUP_DIR / "exceptions.txt.example"

IS_WINDOWS = os.name == "nt"
MIN_PYTHON = (3, 9)

# Which requirement line belongs to which chat platform, so we install only the
# backend you actually run. `--all-backends` installs both.
BACKEND_PACKAGE = {"telegram": "python-telegram-bot", "discord": "discord.py"}

# Each platform's token setting, and the plain-text file config.py falls back
# to when the setting is empty.
TOKEN_KEY = {"telegram": "TELEGRAM_BOT_TOKEN", "discord": "DISCORD_BOT_TOKEN"}
TOKEN_FILE = {"TELEGRAM_BOT_TOKEN": "token.txt", "DISCORD_BOT_TOKEN": "discord_token.txt"}


# --------------------------------------------------------------------------- #
# Console helpers
# --------------------------------------------------------------------------- #
def say(msg=""):
    print(msg, flush=True)


def step(n, total, title):
    say()
    say(f"── Step {n}/{total}: {title} " + "─" * max(0, 58 - len(title)))


def die(msg):
    say()
    say(f"ERROR: {msg}")
    raise SystemExit(1)


CLEAR = "-"  # type this at a prompt to blank out the value in brackets


def ask(prompt, default="", secret=False):
    """Free-text prompt. Enter accepts the default shown in brackets; `-`
    clears it (so a value from a previous run can actually be removed).
    A `secret` default is shown masked, as in the conf.env listing."""
    shown = mask(default) if secret else default
    suffix = f" [{shown}] ({CLEAR} to clear)" if default else " []"
    try:
        answer = input(f"  {prompt}{suffix}: ").strip()
    except EOFError:
        die("No input available — run this from an interactive terminal.")
    if answer == CLEAR:
        return ""
    return answer or default


def ask_number(prompt, default, cast):
    """A number >= 0. A cleared answer ("") is returned as-is so the caller
    can fall back to the built-in default."""
    while True:
        raw = ask(prompt, default)
        if raw == "":
            return raw
        try:
            value = cast(raw)
        except ValueError:
            say(f"    ! '{raw}' isn't a number — try again.")
            continue
        if value < 0:
            say("    ! Must be 0 or more — try again.")
            continue
        return raw


def ask_yes_no(prompt, default=False):
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            answer = input(f"  {prompt} [{hint}]: ").strip().lower()
        except EOFError:
            die("No input available — run this from an interactive terminal.")
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def ask_choice(prompt, options):
    """options: list of (value, label). Returns the chosen value."""
    for i, (_, label) in enumerate(options, 1):
        say(f"    {i}) {label}")
    while True:
        raw = ask(prompt, "1")
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        say(f"    ! Enter a number between 1 and {len(options)}.")


def run(cmd, **kwargs):
    """Run a command, letting it print straight to this terminal."""
    say(f"  $ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, **kwargs)


# --------------------------------------------------------------------------- #
# 1. Preflight
# --------------------------------------------------------------------------- #
def preflight():
    if sys.version_info < MIN_PYTHON:
        die(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required; this is "
            f"{sys.version.split()[0]}."
        )
    missing = [
        p.name for p in (HERE / "ytwatchlistbot.py", REQUIREMENTS) if not p.exists()
    ]
    if missing:
        die(
            f"{', '.join(missing)} not found in {HERE}. Keep this script in "
            "the project's manual-setup/ folder."
        )
    if not sys.stdin.isatty():
        die("This script is interactive — run it from a terminal.")
    say(f"  Python {sys.version.split()[0]} at {sys.executable}")
    say(f"  Project folder: {HERE}")


# --------------------------------------------------------------------------- #
# 2. Chat platform
# --------------------------------------------------------------------------- #
def configured_token(existing, key):
    """The token config.py would load for `key`: the conf.env value, else the
    contents of its token file (token.txt / discord_token.txt)."""
    token = existing.get(key, "").strip()
    if token:
        return token
    token_file = HERE / TOKEN_FILE[key]
    if token_file.exists():
        return token_file.read_text(encoding="utf-8-sig").strip()
    return ""


def configured_platform(existing):
    """The platform an existing conf.env would start on — the same rule as
    config.py: CHAT_PLATFORM if set, else Discord when only a Discord token
    is present (in conf.env or its token file), else Telegram. Returns ""
    when nothing decides it."""
    explicit = existing.get("CHAT_PLATFORM", "").strip().lower()
    if explicit in ("telegram", "discord"):
        return explicit
    if explicit:
        return "telegram"  # config.py warns and falls back to telegram
    telegram = configured_token(existing, "TELEGRAM_BOT_TOKEN")
    if configured_token(existing, "DISCORD_BOT_TOKEN") and not telegram:
        return "discord"
    if telegram:
        return "telegram"
    return ""


def choose_platform(existing):
    current = configured_platform(existing)
    if current:
        say(f"  conf.env currently runs on {current}.")
    say("  Which chat app should the bot talk to?")
    default_first = current == "discord"
    options = (
        [("discord", "Discord"), ("telegram", "Telegram")]
        if default_first
        else [("telegram", "Telegram"), ("discord", "Discord")]
    )
    return ask_choice("Pick 1 or 2", options)


# --------------------------------------------------------------------------- #
# 3. Virtualenv + dependencies
# --------------------------------------------------------------------------- #
def venv_python():
    return VENV_DIR / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def requirement_lines(chat_platform, all_backends):
    """requirements.txt minus the backend you're not using."""
    skip = ""
    if not all_backends:
        other = "discord" if chat_platform == "telegram" else "telegram"
        skip = BACKEND_PACKAGE[other]
    reqs = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if skip and line.startswith(skip):
            continue
        reqs.append(line)
    return reqs


def make_venv(chat_platform, all_backends):
    if venv_python().exists():
        say(f"  Reusing the existing venv at {VENV_DIR} (dependencies refreshed).")
    else:
        if VENV_DIR.exists():
            die(
                f"{VENV_DIR} exists but has no Python in it. Delete it and re-run "
                "this script."
            )
        say(f"  Creating {VENV_DIR} ...")
        if run([sys.executable, "-m", "venv", str(VENV_DIR)]).returncode != 0:
            die("Couldn't create the virtualenv (is the venv module installed?).")

    vpy = venv_python()
    run([str(vpy), "-m", "pip", "install", "--upgrade", "pip"],
        stdout=subprocess.DEVNULL)
    reqs = requirement_lines(chat_platform, all_backends)
    say(f"  Installing: {', '.join(reqs)}")
    if run([str(vpy), "-m", "pip", "install", *reqs]).returncode != 0:
        die("Dependency installation failed — see pip's output above.")
    say("  Dependencies installed.")
    return vpy


# --------------------------------------------------------------------------- #
# 4. conf.env
# --------------------------------------------------------------------------- #
class Setting:
    """One conf.env line. `default` is what config.py falls back to."""

    def __init__(self, key, prompt, default="", required=False, kind="text"):
        self.key = key
        self.prompt = prompt
        self.default = default
        self.required = required
        self.kind = kind  # text | int | float


def settings_for(chat_platform):
    if chat_platform == "telegram":
        chat = [
            Setting(
                "TELEGRAM_BOT_TOKEN",
                "Telegram bot token (from @BotFather)",
                required=True,
            ),
            Setting(
                "TELEGRAM_CHAT_ID",
                "Chat id to notify (blank = learned from your first /start)",
            ),
        ]
    else:
        chat = [
            Setting(
                "DISCORD_BOT_TOKEN",
                "Discord bot token (Developer Portal -> Bot -> Reset Token)",
                required=True,
            ),
            Setting(
                "DISCORD_CHANNEL_ID",
                "Channel id to post in (blank = learned from your first /start)",
            ),
            Setting(
                "DISCORD_GUILD_ID",
                "Your server id (blank = commands register globally, up to ~1h)",
            ),
        ]
    return chat + [
        Setting("CHECK_INTERVAL_MINUTES", "Check every N minutes", "30", kind="int"),
        Setting(
            "YOUTUBE_PLAYLIST_NAME",
            "Playlist the Save button fills",
            "Telegram Watchlist",
        ),
        Setting("YOUTUBE_API_KEY", "YouTube Data API key (blank = scraping fallback)"),
        Setting(
            "MAX_VIDEO_AGE_DAYS",
            "Ignore videos older than N days (0 disables)",
            "14",
            kind="int",
        ),
        Setting("FEED_FETCH_RETRIES", "Retries per feed on a failure", "3", kind="int"),
        Setting(
            "FEED_FETCH_BACKOFF", "Retry backoff base, seconds", "1", kind="float"
        ),
        Setting(
            "FEED_FETCH_PACING_SECONDS",
            "Pause between channel feeds, seconds",
            "0.5",
            kind="float",
        ),
        Setting("EXCEPTIONS_FILE", "Per-channel opt-out file", "exceptions.txt"),
        Setting("ARCHIVE_FILE", "Archive/state file", "archive.json"),
        Setting("LOG_DIR", "Folder for daily log files", "logs"),
        Setting(
            "YOUTUBE_CLIENT_SECRET_FILE",
            "Google OAuth client file",
            "client_secret.json",
        ),
        Setting("YOUTUBE_TOKEN_FILE", "YouTube token file", "youtube_token.json"),
        Setting(
            "YOUTUBE_PLAYLIST_STATE_FILE",
            "Cached playlist-id file",
            "youtube_state.json",
        ),
    ]


def read_conf(path):
    """Parse KEY=value from an existing conf.env (comments ignored)."""
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if not key.strip():
            continue  # "=value" has no name; config.py skips it too
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def mask(value):
    return f"…{value[-4:]}" if len(value) > 4 else "(set)"


def is_secret(key):
    """Settings whose value is never printed in full (tokens, the API key).
    YOUTUBE_TOKEN_FILE is only a path, so it stays readable."""
    return ("TOKEN" in key or "API_KEY" in key) and not key.endswith("_FILE")


def show_existing(values):
    say("  conf.env already exists:")
    for key, val in values.items():
        shown = mask(val) if is_secret(key) else val
        say(f"    {key}={shown}")


def collect_settings(chat_platform, existing):
    """Walk through every setting; Enter keeps the value in brackets."""
    say("  Press Enter to accept the value in brackets.")
    say()
    answers = {}
    for s in settings_for(chat_platform):
        default = existing.get(s.key, s.default)
        while True:
            if s.kind in ("int", "float"):
                value = ask_number(
                    s.prompt, default, int if s.kind == "int" else float
                )
            else:
                value = ask(s.prompt, default, secret=is_secret(s.key))
            if s.required and not value:
                say("    ! This one is required.")
                continue
            if not value and s.kind in ("int", "float"):
                value = s.default  # cleared -> back to the built-in default
            break
        answers[s.key] = value
    return answers


def _line_key(line):
    """The KEY of a `KEY=value` line (stripped), or None without an `=`."""
    if "=" not in line:
        return None
    return line.partition("=")[0].strip()


def write_private(path, text):
    """Write a file that holds secrets (conf.env carries the bot token) so
    only this user can read it: 0600, also when the file already existed with
    a wider mode. The mode does nothing on Windows."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.chmod(path, 0o600)
        f.write(text)


def set_conf_key(path, key, value):
    """Set KEY=value, leaving exactly one active line for KEY.

    config.py and systemd's EnvironmentFile= both take the *last* active
    occurrence, but two active lines are still a trap for whoever edits the
    file next. The last active line is replaced and any other active ones are
    commented out; with no active line, the first commented `# KEY=` is
    uncommented; otherwise the line is appended.
    """
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    # Keys are compared stripped, as read_conf and config.py parse them, so a
    # hand-written `KEY = value` line counts as active too.
    active = [
        i for i, l in enumerate(lines)
        if not l.strip().startswith("#") and _line_key(l) == key
    ]
    if active:
        for i in active[:-1]:
            lines[i] = f"# {lines[i].strip()}"
        lines[active[-1]] = f"{key}={value}"
    else:
        for i, line in enumerate(lines):
            if _line_key(line.lstrip("# ")) == key:
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    write_private(path, "\n".join(lines) + "\n")


def backup_conf():
    """Copy conf.env to conf.env.bak before this script changes it."""
    if CONF.exists():
        backup = CONF.with_suffix(".env.bak")
        shutil.copy2(CONF, backup)
        os.chmod(backup, 0o600)  # it holds the same token
        say(f"  Kept your previous config as {backup.name}.")


def write_conf(chat_platform, answers, existing=None):
    """Write conf.env. Anything left at its default is written commented out,
    so the file records the default without pinning it. Keys in the existing
    file that this script doesn't ask about (the other platform's token,
    YOUTUBE_DEVICE_CLIENT_SECRET_FILE, ...) are carried over unchanged."""
    backup_conf()

    defaults = {s.key: s.default for s in settings_for(chat_platform)}
    required = {s.key for s in settings_for(chat_platform) if s.required}

    lines = [
        f"# Written by manual-setup/setup_bot.py on {datetime.now():%Y-%m-%d %H:%M}.",
        "# Every setting is listed; commented lines are at their default value.",
        "# See manual-setup/conf.env.example for what each one does.",
        "",
        f"CHAT_PLATFORM={chat_platform}",
        "",
    ]
    for key, value in answers.items():
        if key in required or (value and value != defaults.get(key, "")):
            lines.append(f"{key}={value}")
        else:
            lines.append(f"# {key}={value}")
    extra = {
        k: v for k, v in (existing or {}).items()
        if k not in answers and k != "CHAT_PLATFORM"
    }
    if extra:
        lines.append("")
        lines.append("# Kept from your previous conf.env:")
        lines.extend(f"{k}={v}" for k, v in extra.items())
    write_private(CONF, "\n".join(lines) + "\n")
    say(f"  Wrote {CONF.name}.")


# --------------------------------------------------------------------------- #
# 5. Token check
# --------------------------------------------------------------------------- #
# On a failure only the exception's type is printed: a requests error's text
# carries the request URL, and Telegram's URL contains the bot token.
_CHECK_CODE = {
    "telegram": (
        "import os, requests\n"
        "t = os.environ['YTW_SETUP_TOKEN']\n"
        "try:\n"
        "    r = requests.get(f'https://api.telegram.org/bot{t}/getMe', timeout=15)\n"
        "    d = r.json()\n"
        "except Exception as exc:\n"
        "    print('SKIP', type(exc).__name__)\n"
        "else:\n"
        "    if r.status_code == 200 and d.get('ok'):\n"
        "        print('OK', '@' + (d['result'].get('username') or '?'))\n"
        "    else:\n"
        "        print('FAIL', d.get('description') or r.status_code)\n"
    ),
    "discord": (
        "import os, requests\n"
        "t = os.environ['YTW_SETUP_TOKEN']\n"
        "try:\n"
        "    r = requests.get('https://discord.com/api/v10/users/@me',\n"
        "                     headers={'Authorization': f'Bot {t}'}, timeout=15)\n"
        "except Exception as exc:\n"
        "    print('SKIP', type(exc).__name__)\n"
        "else:\n"
        "    if r.status_code == 200:\n"
        "        print('OK', r.json().get('username') or '?')\n"
        "    elif r.status_code == 401:\n"
        "        print('FAIL', '401 Unauthorized')\n"
        "    else:\n"
        "        print('FAIL', f'HTTP {r.status_code}')\n"
    ),
}


def verify_token(vpy, chat_platform, token):
    """Ask the platform who this token belongs to. Returns True to continue."""
    env = dict(os.environ, YTW_SETUP_TOKEN=token)
    proc = subprocess.run(
        [str(vpy), "-c", _CHECK_CODE[chat_platform]],
        env=env, capture_output=True, text=True,
    )
    verdict, _, detail = proc.stdout.strip().partition(" ")
    if verdict == "OK":
        say(f"  Token works — connected as {detail}.")
        return True
    if verdict == "SKIP":
        say(f"  Couldn't reach {chat_platform} to check the token ({detail}).")
        say("  Carrying on — the bot will tell you at startup if it's wrong.")
        return True
    say(f"  {chat_platform.title()} rejected the token: {detail or proc.stderr.strip()}")
    return ask_yes_no("Keep it anyway?", default=False)


# --------------------------------------------------------------------------- #
# 6/7. YouTube connection and the service
# --------------------------------------------------------------------------- #
def project_path(value, default):
    """A file setting resolved like config._path: relative to the project
    folder, and an empty value means the default (not the folder itself)."""
    return HERE / ((value or "").strip() or default)


def youtube_step(vpy, answers):
    client_secret = project_path(
        answers.get("YOUTUBE_CLIENT_SECRET_FILE"), "client_secret.json"
    )
    token_file = project_path(answers.get("YOUTUBE_TOKEN_FILE"), "youtube_token.json")

    if token_file.exists():
        say(f"  {token_file.name} already exists — YouTube is connected.")
        if not ask_yes_no("Re-run the authorization anyway?", default=False):
            return
    if not client_secret.exists():
        say(f"  {client_secret.name} isn't here yet, so authorization can't run.")
        say("  Create an OAuth *Desktop app* client in the Google Cloud Console")
        say("  (with YouTube Data API v3 enabled), publish its OAuth consent")
        say("  screen (left in Testing, Google expires the token every 7 days),")
        say(f"  save the JSON to {client_secret}, then run:")
        say(f"      {vpy} {AUTHORIZE}")
        return
    if not ask_yes_no("Connect your YouTube account now?", default=True):
        say(f"  Skipped. Run it later with:  {vpy} {AUTHORIZE}")
        return
    # authorize_youtube.py detects a headless server itself and switches to its
    # console flow, so it just gets the terminal.
    run([str(vpy), str(AUTHORIZE)], cwd=HERE)


def exceptions_step(answers):
    # The file the bot will actually read (EXCEPTIONS_FILE, like config.py).
    exceptions = project_path(answers.get("EXCEPTIONS_FILE"), "exceptions.txt")
    if exceptions.exists() or not EXCEPTIONS_EXAMPLE.exists():
        return
    if ask_yes_no(
        f"Create {exceptions.name} from the example (per-channel opt-outs)?",
        default=False,
    ):
        shutil.copy2(EXCEPTIONS_EXAMPLE, exceptions)
        say(f"  Copied manual-setup/{EXCEPTIONS_EXAMPLE.name} -> {exceptions}.")


def service_step():
    """Offer the systemd install. Returns False if it was tried and failed."""
    installer = SETUP_DIR / "deploy" / "install-service.sh"
    if not sys.platform.startswith("linux") or not installer.exists():
        return None
    if not shutil.which("systemctl"):
        say("  systemd isn't available here — skipping the service install.")
        return None
    if not shutil.which("sudo"):
        say("  sudo isn't installed here, so the service can't be installed from")
        say("  this script. As root, run:")
        say(f"      SERVICE_USER=<your user> bash {installer}")
        return None
    say("  A systemd service keeps the bot running and starts it on boot.")
    if not ask_yes_no("Install it now (needs sudo)?", default=False):
        say(f"  Skipped. Run it later with:  sudo bash {installer}")
        return None
    if run(["sudo", "bash", str(installer)]).returncode != 0:
        say("  The service install didn't succeed — see its output above.")
        return False
    return True


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(
        description="Set up YtWatchlistBot: venv, dependencies, conf.env, OAuth."
    )
    parser.add_argument(
        "--all-backends",
        action="store_true",
        help="install both chat libraries, not just the one you pick",
    )
    args = parser.parse_args()
    # Output piped or redirected on Windows uses the ANSI code page, which has
    # no box-drawing characters or emoji: show "?" instead of crashing.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    total = 7
    say()
    say("YtWatchlistBot setup")
    say("=" * 60)

    step(1, total, "Checking your environment")
    preflight()

    existing = read_conf(CONF)
    step(2, total, "Choosing a chat platform")
    chat_platform = choose_platform(existing)
    say(f"  Using {chat_platform}.")

    step(3, total, "Virtualenv and dependencies")
    vpy = make_venv(chat_platform, args.all_backends)

    step(4, total, "Settings (conf.env)")
    answers = existing
    write_it = True
    changed_conf = False
    new_token = None  # (key, value) missing from a kept conf.env, written after step 5
    if CONF.exists():
        show_existing(existing)
        write_it = ask_yes_no("Go through the settings and rewrite it?", default=False)
        if not write_it:
            say("  Keeping conf.env as it is.")
            configured = configured_platform(existing) or "telegram"
            if configured != chat_platform:
                say(
                    f"  Note: as it stands conf.env starts the bot on {configured}, "
                    f"not {chat_platform}."
                )
                if ask_yes_no(
                    f"Set CHAT_PLATFORM={chat_platform} in it?", default=True
                ):
                    # The new-token write after step 5 comes later; this one
                    # backup keeps the file as it was before either change.
                    backup_conf()
                    set_conf_key(CONF, "CHAT_PLATFORM", chat_platform)
                    changed_conf = True
                    say(f"  conf.env now uses {chat_platform}.")
                else:
                    chat_platform = configured
                    say(f"  Leaving it on {configured}.")
                    # Step 3 installed the library for the platform picked
                    # there; the bot will start on this one, so it needs its own.
                    if not args.all_backends:
                        vpy = make_venv(chat_platform, args.all_backends)
            # The kept conf.env may have no token for the platform it starts
            # on (a switched one, or an example copied but never filled in);
            # without one the bot can't start, so ask for it now.
            token_key = TOKEN_KEY[chat_platform]
            if not configured_token(existing, token_key):
                token_prompt = settings_for(chat_platform)[0].prompt
                token = ""
                while not token:
                    token = ask(token_prompt)
                    if not token:
                        say("    ! This one is required.")
                existing[token_key] = token
                new_token = (token_key, token)
    if write_it:
        answers = collect_settings(chat_platform, existing)

    step(5, total, "Checking the bot token")
    token_key = TOKEN_KEY[chat_platform]
    token = configured_token(answers, token_key)
    if not token:
        say(f"  No {token_key} to check.")
    elif not verify_token(vpy, chat_platform, token):
        say()
        if changed_conf:
            say(
                f"  Only CHAT_PLATFORM was changed in {CONF.name} (the original is "
                f"in {CONF.name}.bak); nothing else was written."
            )
        else:
            say("  Nothing was written.")
        say("  Re-run this script with a working token.")
        raise SystemExit(1)
    if write_it:
        write_conf(chat_platform, answers, existing)
    elif new_token:
        if not changed_conf:
            backup_conf()  # a switch above already kept the original
        set_conf_key(CONF, *new_token)
        say(f"  Added {new_token[0]} to {CONF.name}.")

    step(6, total, "Connecting YouTube (your watchlist + the Save button)")
    youtube_step(vpy, answers)
    exceptions_step(answers)

    step(7, total, "Running it as a service")
    service_ok = service_step()

    launcher = venv_python()
    say()
    say("=" * 60)
    if service_ok is False:
        say("Setup finished, but the service install failed (see above).")
        say("Fix that and re-run the installer, or start the bot by hand with:")
    else:
        say("Setup complete. Start the bot with:")
    say(f"    {launcher} {HERE / 'ytwatchlistbot.py'}")
    say()
    if chat_platform == "telegram":
        say("Then message your bot on Telegram and send /start.")
    else:
        say("Then run /start in the Discord channel you want notifications in.")
    say()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say()
        say("Cancelled — nothing further was changed.")
        raise SystemExit(130)
