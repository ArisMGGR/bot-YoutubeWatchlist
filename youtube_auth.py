"""Google's OAuth 2.0 *device* flow, shared by `manual-setup/authorize_youtube.py --device`
and the bots' `/authorize` command.

The device flow is the one smart TVs use: Google hands out a short user code
and a `google.com/device` URL, you approve on any device, and the process that
asked polls until the token arrives. Nothing sensitive is typed anywhere —
which is why it's the flow used from chat.

Three steps, kept separate so a bot can await between polls without blocking
its event loop:

    flow  = start(secret_file)          # -> DeviceFlow (code + URL to show)
    creds = poll(flow)                  # -> Credentials, or None while pending
    save(creds)                         # -> writes youtube_token.json

All functions are blocking (plain HTTPS); call them from a worker thread.
"""
import json
import os
import time
from dataclasses import dataclass

import requests

import config
import youtube_playlist

# Must match youtube_playlist.SCOPES.
SCOPES = ["https://www.googleapis.com/auth/youtube"]

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class DeviceAuthError(Exception):
    """The device flow failed; the message is user-facing."""


class ClientRejected(DeviceAuthError):
    """This OAuth client can't use the device flow (needs a 'TVs and Limited
    Input devices' client)."""


class Denied(DeviceAuthError):
    """You clicked Cancel/Deny on Google's page."""


class Expired(DeviceAuthError):
    """The code expired before it was approved."""


@dataclass
class DeviceFlow:
    user_code: str
    verification_url: str
    device_code: str
    interval: float          # seconds between polls (Google may raise it)
    expires_at: float        # time.monotonic() deadline
    client_id: str
    client_secret: str

    @property
    def seconds_left(self):
        return max(0.0, self.expires_at - time.monotonic())


def read_client_config(path):
    """Return (client_id, client_secret) from a Google OAuth client-secret JSON.

    Handles the usual `{"installed": {...}}` / `{"web": {...}}` wrappers as well
    as an unwrapped object, so the same file works whether it was downloaded as a
    Desktop-app or a "TVs and Limited Input devices" client.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:  # unreadable, or not JSON at all
        raise DeviceAuthError(
            f"Couldn't read {path} as an OAuth client-secret file ({exc}). "
            "Download the JSON again from the Google Cloud Console."
        ) from None
    if not isinstance(data, dict):
        data = {}
    for wrapper in ("installed", "web"):
        if isinstance(data.get(wrapper), dict):
            data = data[wrapper]
            break
    try:
        return data["client_id"], data["client_secret"]
    except KeyError:
        raise DeviceAuthError(
            f"{path} doesn't look like an OAuth client-secret file "
            "(no client_id/client_secret)."
        ) from None


def _error_code(resp):
    """Best-effort `error` field from a non-OK OAuth JSON response, else ''."""
    if resp.ok:
        return ""
    try:
        return resp.json().get("error", "")
    except ValueError:
        return ""


def start(secret_file=None):
    """Ask Google for a device code. Returns a :class:`DeviceFlow` to display.

    Raises :class:`ClientRejected` if the client can't use the device flow,
    :class:`DeviceAuthError` for a missing/unreadable secret file or any other
    failure to start.
    """
    secret_file = secret_file or config.YOUTUBE_DEVICE_CLIENT_SECRET_FILE
    if not os.path.exists(secret_file):
        raise DeviceAuthError(
            f"{os.path.basename(secret_file)} isn't on the server (looked in "
            f"{secret_file}). Download your OAuth client's JSON from the Google "
            "Cloud Console and save it there."
        )
    client_id, client_secret = read_client_config(secret_file)

    try:
        resp = requests.post(
            DEVICE_CODE_URL,
            data={"client_id": client_id, "scope": " ".join(SCOPES)},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise DeviceAuthError(f"Couldn't reach Google to start authorization: {exc}") from exc
    if resp.status_code in (401, 403) or _error_code(resp) in (
        "invalid_client",
        "unauthorized_client",
    ):
        raise ClientRejected(
            "This OAuth client doesn't support the device flow. In the Google "
            "Cloud Console create an OAuth client of type 'TVs and Limited Input "
            "devices' (same project, YouTube Data API v3 enabled), download its "
            "JSON, and point YOUTUBE_DEVICE_CLIENT_SECRET_FILE at it — or run "
            "`python manual-setup/authorize_youtube.py` on the server for the browser flow."
        )
    if not resp.ok:
        raise DeviceAuthError(
            f"Couldn't start device authorization ({resp.status_code}): {resp.text}"
        )
    info = resp.json()
    return DeviceFlow(
        user_code=info["user_code"],
        verification_url=info.get("verification_url") or info.get("verification_uri"),
        device_code=info["device_code"],
        interval=float(info.get("interval", 5)),
        expires_at=time.monotonic() + float(info.get("expires_in", 1800)),
        client_id=client_id,
        client_secret=client_secret,
    )


def poll(flow):
    """One poll of Google's token endpoint.

    Returns ``Credentials`` once you've approved, ``None`` while still waiting
    (``flow.interval`` is bumped if Google asks us to slow down). Raises
    :class:`Denied`, :class:`Expired`, or :class:`DeviceAuthError`.
    """
    if flow.seconds_left <= 0:
        raise Expired("The code expired before it was approved.")
    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": flow.client_id,
                "client_secret": flow.client_secret,
                "device_code": flow.device_code,
                "grant_type": _DEVICE_GRANT,
            },
            timeout=30,
        )
    except requests.RequestException:
        return None  # transient — just try again next interval
    try:
        payload = resp.json() if resp.content else {}
    except ValueError:
        payload = {}
    if resp.ok:
        return _credentials(flow, payload)
    error = payload.get("error")
    if error == "authorization_pending":
        return None
    if error == "slow_down":
        flow.interval += 5
        return None
    if error == "access_denied":
        raise Denied("Access was denied on Google's page. Nothing was changed.")
    if error == "expired_token":
        raise Expired("The code expired before it was approved.")
    raise DeviceAuthError(f"Authorization failed: {payload or resp.text}")


def wait(flow, cancelled=lambda: False):
    """Blocking: poll until approved. `cancelled()` is checked between polls."""
    while True:
        time.sleep(flow.interval)
        if cancelled():
            return None
        creds = poll(flow)
        if creds is not None:
            return creds


def _credentials(flow, payload):
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        token_uri=TOKEN_URL,
        client_id=flow.client_id,
        client_secret=flow.client_secret,
        scopes=SCOPES,
    )


def save(creds):
    """Write the token file (atomically, readable only by this user) and
    forget the cached playlist id.

    The playlist cache belongs to whichever channel was connected before; a
    re-authorization may pick a different one, so it's re-found or re-created
    on the next Save.
    """
    youtube_playlist.write_private(config.YOUTUBE_TOKEN_FILE, creds.to_json())
    try:
        os.remove(config.YOUTUBE_PLAYLIST_STATE_FILE)
    except OSError:
        pass
