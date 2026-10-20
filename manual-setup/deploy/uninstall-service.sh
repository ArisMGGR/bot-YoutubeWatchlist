#!/usr/bin/env bash
#
# Remove the YtWatchlistBot systemd service: stop it, disable autostart, and
# delete the installed unit file (plus any drop-in overrides).
#
# This ONLY touches the systemd service. Your project files, conf.env, and runtime
# state (archive.json / youtube_state.json) are left untouched.
#
# Usage:
#   sudo bash manual-setup/deploy/uninstall-service.sh          # asks for confirmation
#   sudo bash manual-setup/deploy/uninstall-service.sh --yes    # skip the prompt (-y also works)
#
set -euo pipefail

# The current service, plus the legacy name it used to ship under, so a
# leftover old unit from before the rename gets cleaned up too.
SERVICES=("youtube-watchlist-bot.service" "ytwatchlistbot.service")
UNIT_DIR="/etc/systemd/system"

# Parse args before the root check, so --help (or a typo) needs no sudo.
# Skip the confirmation prompt with -y/--yes.
assume_yes=false
for arg in "$@"; do
  case "$arg" in
    -y | --yes) assume_yes=true ;;
    -h | --help)
      # Print the leading comment block only (skip the shebang, stop at code).
      awk 'NR==1 && /^#!/ {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

# Re-run with sudo if we're not root — deleting from ${UNIT_DIR} needs it.
if [[ ${EUID} -ne 0 ]]; then
  echo "Root is required to modify ${UNIT_DIR}; re-running with sudo..."
  # Through bash, not "$0" itself: the script may have no executable bit.
  exec sudo -- bash "$0" "$@"
fi

if [[ "${assume_yes}" != true ]]; then
  read -r -p "Stop and permanently delete the YtWatchlistBot service? [y/N] " reply
  case "${reply}" in
    y | Y | yes | YES) ;;
    *)
      echo "Aborted. Nothing was changed."
      exit 0
      ;;
  esac
fi

removed_any=false
for service in "${SERVICES[@]}"; do
  unit_path="${UNIT_DIR}/${service}"

  # Nothing installed under this name — skip quietly.
  if [[ ! -e "${unit_path}" ]] && ! systemctl list-unit-files "${service}" >/dev/null 2>&1; then
    continue
  fi

  echo "==> Stopping and disabling ${service} (if active)..."
  systemctl disable --now "${service}" 2>/dev/null || true

  echo "==> Deleting ${unit_path} and any drop-ins..."
  rm -f "${unit_path}"
  rm -rf "${unit_path}.d"
  removed_any=true
done

if [[ "${removed_any}" != true ]]; then
  echo "Nothing to do — no YtWatchlistBot service was installed."
  exit 0
fi

echo "==> Reloading systemd and clearing failed state..."
systemctl daemon-reload
for service in "${SERVICES[@]}"; do
  systemctl reset-failed "${service}" 2>/dev/null || true
done

# Verify the unit files are gone.
echo "==> Verifying removal..."
leftovers=false
for service in "${SERVICES[@]}"; do
  if [[ -e "${UNIT_DIR}/${service}" ]]; then
    echo "    WARNING: ${UNIT_DIR}/${service} still exists."
    leftovers=true
  fi
done

if [[ "${leftovers}" == true ]]; then
  echo "Some files remain — check the warnings above." >&2
  exit 1
fi

echo "    Service removed. Project files and data were left untouched."
