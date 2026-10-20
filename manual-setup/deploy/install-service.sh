#!/usr/bin/env bash
#
# Auto-install the YtWatchlistBot systemd service with no prompts.
#
# It figures out everything itself and fills in the unit template:
#   * User=              the account that ran sudo ($SUDO_USER, else whoami)
#   * WorkingDirectory=  this repo's root (two levels up from manual-setup/deploy/)
#   * ExecStart python   <repo>/venv/bin/python3 if present, else system python3
#   * EnvironmentFile=   <repo>/conf.env
#
# MANDATORY:
#   Run it with sudo from your normal login (root is needed to write
#   /etc/systemd/system, but the service User= comes from the account that
#   invoked sudo — so don't run it from a root shell):
#
#       sudo bash manual-setup/deploy/install-service.sh
#
# Optional overrides (still no prompts):
#   SERVICE_USER=someuser   force the account the service runs as
#   PYTHON=/path/to/python3 force the interpreter
#   SETTLE_SECONDS=8        how long to watch the service before reporting
#                           whether it stayed up (default 8)
#   --dry-run              print what would be installed, change nothing
#   -y, --yes               accepted for symmetry with uninstall (this installer
#                           never prompts anyway)
#
set -euo pipefail

SERVICE="youtube-watchlist-bot.service"
UNIT_DIR="/etc/systemd/system"
UNIT_DEST="${UNIT_DIR}/${SERVICE}"
SETTLE_SECONDS="${SETTLE_SECONDS:-8}"

# --- Locate this script and the repo root -----------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
TEMPLATE="${SCRIPT_DIR}/${SERVICE}.example"

# --- Parse args (before the root check, so --dry-run/--help need no sudo) ----
dry_run=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=true ;;
    -y | --yes) ;; # no-op: this installer never prompts
    -h | --help)
      awk 'NR==1 && /^#!/ {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

# --- Root is required to write the unit (skip for --dry-run) -----------------
if [[ "${dry_run}" != true && ${EUID} -ne 0 ]]; then
  echo "Root is required to write ${UNIT_DIR}; re-running with sudo..."
  # Through bash, not "$0" itself: the script may have no executable bit
  # (e.g. committed from Windows), and sudo would then refuse to run it.
  exec sudo --preserve-env=SERVICE_USER,PYTHON,SETTLE_SECONDS -- bash "$0" "$@"
fi

# --- Decide which account the service runs as -------------------------------
# Under sudo, $SUDO_USER is the real user; whoami would just say 'root'.
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(whoami)}}"

if [[ "${SERVICE_USER}" == "root" ]]; then
  cat >&2 <<EOF
ERROR: Could not determine a non-root user to run the bot as.
Run this with sudo from your normal login:
    sudo bash manual-setup/deploy/install-service.sh
or name the user explicitly (still no prompt):
    sudo SERVICE_USER=youruser bash manual-setup/deploy/install-service.sh
EOF
  exit 1
fi

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  echo "ERROR: user '${SERVICE_USER}' does not exist on this machine." >&2
  exit 1
fi

# --- Pick the python interpreter --------------------------------------------
if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_BIN="${PYTHON}"
elif [[ -x "${PROJECT_DIR}/venv/bin/python3" ]]; then
  PYTHON_BIN="${PROJECT_DIR}/venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
else
  echo "ERROR: no python3 found (no ${PROJECT_DIR}/venv and python3 not on PATH)." >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ERROR: python interpreter '${PYTHON_BIN}' is not executable." >&2
  exit 1
fi

# --- Template must exist (single source of truth for the unit) --------------
if [[ ! -f "${TEMPLATE}" ]]; then
  echo "ERROR: template not found: ${TEMPLATE}" >&2
  exit 1
fi

# --- Non-blocking sanity warning --------------------------------------------
if [[ ! -f "${PROJECT_DIR}/conf.env" ]]; then
  echo "WARNING: ${PROJECT_DIR}/conf.env not found — the bot needs TELEGRAM_BOT_TOKEN."
  echo "         Installing anyway; the service will crash-loop until you add it."
fi

# --- Render the unit from the template --------------------------------------
# Drop the template's 'copy me' header (everything before [Unit]), then fill in
# the machine-specific lines.
TMP_UNIT="$(mktemp)"
trap 'rm -f "${TMP_UNIT}"' EXIT

# Escape what sed treats specially in a replacement (\, & and our | delimiter),
# so a path like /home/me/R&D/bot is written as-is.
sed_escape() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }
E_USER="$(sed_escape "${SERVICE_USER}")"
E_PROJECT="$(sed_escape "${PROJECT_DIR}")"
E_PYTHON="$(sed_escape "${PYTHON_BIN}")"

sed -n '/^\[Unit\]/,$p' "${TEMPLATE}" \
  | sed -e "s|^User=.*|User=${E_USER}|" \
        -e "s|^WorkingDirectory=.*|WorkingDirectory=${E_PROJECT}|" \
        -e "s|^ExecStart=.*|ExecStart=\"${E_PYTHON}\" ytwatchlistbot.py|" \
        -e "s|^EnvironmentFile=.*|EnvironmentFile=-${E_PROJECT}/conf.env|" \
  > "${TMP_UNIT}"

echo "==> Resolved configuration:"
echo "      User=${SERVICE_USER}"
echo "      WorkingDirectory=${PROJECT_DIR}"
echo "      ExecStart=\"${PYTHON_BIN}\" ytwatchlistbot.py"
echo "      EnvironmentFile=-${PROJECT_DIR}/conf.env"

if [[ "${dry_run}" == true ]]; then
  echo
  echo "==> --dry-run: would install the following to ${UNIT_DEST}:"
  echo "----------------------------------------------------------------------"
  cat "${TMP_UNIT}"
  echo "----------------------------------------------------------------------"
  echo "==> --dry-run: would then run daemon-reload, enable, and (re)start."
  exit 0
fi

# --- Install, enable, (re)start ---------------------------------------------
echo "==> Installing ${UNIT_DEST}"
install -m 0644 "${TMP_UNIT}" "${UNIT_DEST}"

echo "==> Enabling and starting ${SERVICE}..."
systemctl daemon-reload
systemctl enable "${SERVICE}" >/dev/null
systemctl restart "${SERVICE}"

# --- Report ------------------------------------------------------------------
# The unit is Type=simple, which counts as "active" the moment the process is
# started, so a bot that exits on a config error (no token, ...) still looks
# active right after the restart. Give it a few seconds, then require the same
# process to still be running: a crash in between leaves it inactive, or
# restarted (Restart=always) under a new PID.
echo "==> Waiting ${SETTLE_SECONDS}s to check that it stays up..."
pid_before="$(systemctl show -p MainPID --value "${SERVICE}")"
sleep "${SETTLE_SECONDS}"
pid_after="$(systemctl show -p MainPID --value "${SERVICE}")"
if systemctl is-active --quiet "${SERVICE}" \
  && [[ "${pid_after}" != "0" && "${pid_after}" == "${pid_before}" ]]; then
  echo "==> ${SERVICE} is active (running)."
  echo "    Follow it live with:  journalctl -u ${SERVICE} -f"
else
  echo "==> ${SERVICE} did NOT come up cleanly. Recent logs:" >&2
  systemctl --no-pager --lines=20 status "${SERVICE}" || true
  echo "    Inspect with:  journalctl -u ${SERVICE} -n 50 --no-pager" >&2
  exit 1
fi
