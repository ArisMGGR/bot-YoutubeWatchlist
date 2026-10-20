"""One-time YouTube authorization for YtWatchlistBot.

Run this once to let the bot add saved videos to a playlist in your YouTube
account. You pick your Google account and approve, and it writes
`youtube_token.json` next to the code. The bot reads that token from then on
(refreshing it automatically) — you don't run this again unless you revoke
access, or the app is still in "Testing" (Google then expires the token after
7 days; see step 3).

Before running:
  1. In the Google Cloud Console, create a project, enable **YouTube Data API
     v3**, and create an **OAuth client ID** of type **Desktop app**.
  2. Download its JSON and save it here as `client_secret.json`
     (or point YOUTUBE_CLIENT_SECRET_FILE at it).
  3. On the OAuth consent screen, **Publish app** (status "In production").
  4. pip install -r requirements.txt

Then:
    python manual-setup/authorize_youtube.py

On a machine with a browser this opens one automatically. Over SSH / on a
headless server there's no browser to open and no local redirect to catch, so
it falls back to a **manual copy-paste flow**: it prints a URL, you open it on
any device (laptop/phone), approve, then paste the resulting code back here.
Force that flow with `--console` (a.k.a. `--no-browser`) if auto-detection gets
it wrong.

**Over SSH, `--device` is smoother:** it prints a short code and a
`google.com/device` URL, you approve on any device, and this script waits and
grabs the token itself — nothing to copy back. Google may require an OAuth
client of type "TVs and Limited Input devices" for it; if so, create one and
point YOUTUBE_DEVICE_CLIENT_SECRET_FILE at its JSON (the error message says so).

    python manual-setup/authorize_youtube.py --device

(You can still do it the old way — run this on your laptop and copy the
resulting `youtube_token.json` to the server next to the code. The OAuth client
is a "Desktop app", so the token isn't tied to any particular machine.)

**Or skip the server entirely:** once the bot is running, send it `/authorize`
in Telegram/Discord — it runs this same device flow from the chat.
"""
import argparse
import os
import sys
from urllib.parse import parse_qs, urlparse

# This script lives in manual-setup/; the bot's modules are one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import youtube_auth  # noqa: E402

# Must match youtube_playlist.SCOPES.
SCOPES = ["https://www.googleapis.com/auth/youtube"]

# Extra OAuth params: offline access + a forced consent prompt guarantee we get
# a refresh token, so the bot can keep working without re-authorizing.
_AUTH_PARAMS = {"access_type": "offline", "prompt": "consent"}

# Guidance shown before either flow starts.
_CHANNEL_TIP = (
    "  1. Sign in with the Google account that owns the channel.\n"
    "  2. If you have more than one channel (e.g. a Brand Account), Google\n"
    "     shows a 'Choose a channel' screen — pick the RIGHT channel here.\n"
    "     Saves go to whichever channel you select now.\n"
)


def _looks_headless():
    """True when this box almost certainly can't run the browser-based flow.

    `run_local_server` opens a browser here and catches the OAuth redirect on
    this machine's localhost. Over SSH with no display neither works: there's
    no browser to open, and a browser on your laptop can't reach this server's
    localhost — so it would just hang forever. Detect that and use the manual
    flow instead. X11/Wayland forwarding (DISPLAY set) runs the browser on this
    host, so the browser flow is fine and we leave it alone.
    """
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return False
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return True
    # A Linux box with no display is headless (e.g. a NAS/Pi run from cron or a
    # bare console). macOS/Windows always have a usable GUI.
    return sys.platform.startswith("linux")


def _extract_code(raw):
    """Pull the OAuth `code` out of a pasted redirect URL, or accept a bare code.

    Accepts the full `http://localhost/?code=...&scope=...` URL from the
    browser's address bar, a bare `code=...&...` query fragment, or just the
    code value on its own. Returns None for a URL that carries no code (e.g.
    the `?error=access_denied` redirect you get after clicking Cancel), so the
    caller can explain that instead of handing Google a bogus code.
    """
    raw = raw.strip()
    if not raw:
        return None
    if "code=" in raw or "error=" in raw or raw.startswith("http"):
        # Full URL -> use its query; bare "code=..." fragment -> use it directly.
        query = urlparse(raw).query or raw
        params = parse_qs(query)
        if params.get("error"):
            sys.exit(
                f"ERROR: Google reported '{params['error'][0]}' — access was not "
                "granted. Re-run and approve the request."
            )
        values = params.get("code")
        return values[0] if values else None
    return raw


def _authorize_console(flow):
    """Headless / SSH flow: no browser here, this machine's localhost unreachable.

    Print the Google consent URL for you to open on any device. After you
    approve, Google redirects the browser to a `http://localhost/...` address
    that won't load (nothing is listening there) — but the authorization code is
    in that URL. Paste the whole URL (or just the code) back here and we exchange
    it for the token.
    """
    # Desktop-app OAuth clients accept any http://localhost[:port] redirect. We
    # never actually serve it; we only read the code out of the redirect URL.
    flow.redirect_uri = "http://localhost"
    auth_url, _ = flow.authorization_url(**_AUTH_PARAMS)
    print(
        "This machine has no browser (SSH / headless), so we'll authorize by\n"
        "copy-paste instead of opening one here.\n\n"
        "1. Open this URL in a browser on ANY device (your laptop or phone):\n\n"
        f"   {auth_url}\n\n"
        f"{_CHANNEL_TIP}\n"
        "3. After approving, your browser tries to open a page like\n"
        "   http://localhost/?code=...  and shows a 'can't reach this page' /\n"
        "   'connection refused' error. That is expected — nothing is running\n"
        "   there. The part you need is in the address bar.\n"
        "4. Copy that FULL localhost URL from the address bar (or just the\n"
        "   code= value) and paste it below.\n\n"
        "   Tip: `--device` skips this copy-paste — it waits for you to approve\n"
        "   and grabs the token itself.\n"
    )
    code = _extract_code(input("Paste the URL or code here: "))
    if not code:
        sys.exit(
            "ERROR: couldn't find an authorization code in what you pasted.\n"
            "Re-run and paste the full http://localhost/?code=... URL from the "
            "browser's address bar."
        )
    flow.fetch_token(code=code)
    return flow.credentials


def _authorize_browser(flow):
    """Local flow: open a browser here and catch the redirect on localhost."""
    print(
        "A browser window will open.\n"
        f"{_CHANNEL_TIP}"
    )
    # Spins up a temporary local web server to catch the OAuth redirect and
    # opens your browser.
    return flow.run_local_server(port=0, **_AUTH_PARAMS)


def _authorize_device(secret_file):
    """Device flow: print a short code + URL, then wait for you to approve.

    Ideal over SSH — no browser here, and nothing to copy back. Google returns a
    `user_code` and a `verification_url`; you enter the code on any device, and
    this script polls until you approve, then receives the token directly.
    The same flow backs the bots' `/authorize` command (see youtube_auth.py).
    """
    # Needed only at the very end, but check now: failing after you've already
    # approved on the other device would throw the granted token away.
    try:
        import google.oauth2.credentials  # noqa: F401
    except ImportError:
        sys.exit(
            "ERROR: google-auth isn't installed.\n"
            "Run: pip install -r requirements.txt"
        )

    try:
        flow = youtube_auth.start(secret_file)
    except youtube_auth.ClientRejected as exc:
        sys.exit(f"ERROR: {exc}")
    except youtube_auth.DeviceAuthError as exc:
        sys.exit(f"ERROR: {exc}")

    print(
        "This machine has no browser (SSH / headless), so authorize on another\n"
        "device — there's nothing to copy back; this waits for you.\n\n"
        f"1. On your laptop or phone, open:  {flow.verification_url}\n"
        f"2. Enter this code:  {flow.user_code}\n\n"
        f"{_CHANNEL_TIP}\n"
        "Waiting for you to approve…  (Ctrl-C to cancel)"
    )
    try:
        creds = youtube_auth.wait(flow)
    except youtube_auth.DeviceAuthError as exc:
        sys.exit(f"ERROR: {exc} Re-run to retry.")
    if not creds.refresh_token:
        print(
            "⚠️  Google didn't return a refresh token — the bot may ask you to "
            "re-authorize later."
        )
    return creds


def parse_args(argv=None):
    """Command-line options. Unknown ones are an error, so `--help` (or a typo
    like `--devcie`) never starts an authorization by accident."""
    parser = argparse.ArgumentParser(
        description="One-time YouTube authorization for YtWatchlistBot "
        "(writes youtube_token.json)."
    )
    parser.add_argument(
        "--console", "--no-browser", dest="console", action="store_true",
        help="copy-paste flow: print a URL, paste the code back (headless / SSH)",
    )
    parser.add_argument(
        "--device", action="store_true",
        help="device flow: enter a short code on google.com/device, nothing to "
        "paste (wins over --console)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    force_console = args.console
    use_device = args.device

    if use_device:
        secret_file = config.YOUTUBE_DEVICE_CLIENT_SECRET_FILE
        if not os.path.exists(secret_file):
            sys.exit(
                f"ERROR: {secret_file} not found.\n"
                "For --device, create an OAuth client (type: 'TVs and Limited "
                "Input devices', or reuse your Desktop-app client) in the Google "
                "Cloud Console, enable YouTube Data API v3, download the JSON, and "
                "save it there (or set YOUTUBE_DEVICE_CLIENT_SECRET_FILE)."
            )
        creds = _authorize_device(secret_file)
    else:
        if not os.path.exists(config.YOUTUBE_CLIENT_SECRET_FILE):
            sys.exit(
                f"ERROR: {config.YOUTUBE_CLIENT_SECRET_FILE} not found.\n"
                "Create an OAuth client ID (type: Desktop app) in the Google Cloud "
                "Console, enable YouTube Data API v3, download the JSON, and save it "
                "there. See this script's docstring for the full steps."
            )

        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError:
            sys.exit(
                "ERROR: google-auth-oauthlib isn't installed.\n"
                "Run: pip install -r requirements.txt"
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            config.YOUTUBE_CLIENT_SECRET_FILE, SCOPES
        )

        if force_console or _looks_headless():
            creds = _authorize_console(flow)
        else:
            creds = _authorize_browser(flow)

    youtube_auth.save(creds)
    print(f"\n✅ Saved {config.YOUTUBE_TOKEN_FILE}")

    # Report which channel the token is bound to, so a wrong pick is obvious.
    try:
        import youtube_playlist
        channel = youtube_playlist.current_channel(creds)
        if channel:
            print(f"🔗 Connected as channel: {channel['title']} ({channel['id']})")
        else:
            print("⚠️  This account has no YouTube channel — create one and re-run.")
    except Exception as exc:  # noqa: BLE001 - best-effort confirmation
        print(f"(Couldn't read the channel name: {exc})")

    print(
        f"\nThe bot will add 💾-saved videos to the '{config.YOUTUBE_PLAYLIST_NAME}' "
        "playlist on that channel.\nIf it's the wrong channel, delete "
        f"{os.path.basename(config.YOUTUBE_TOKEN_FILE)} and "
        f"{os.path.basename(config.YOUTUBE_PLAYLIST_STATE_FILE)}, then re-run this."
    )


if __name__ == "__main__":
    main()
