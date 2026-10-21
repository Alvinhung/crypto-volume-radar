#!/bin/sh
# Load .env (Telegram token etc.) if present, then run one of the scripts.
#   ./run.sh radar.py [args]      ./run.sh alerts.py      ./run.sh onchain-alert.py
cd "$(dirname "$0")" || exit 1
[ -f ./.env ] && { set -a; . ./.env; set +a; }
script="${1:?usage: run.sh <script.py> [args]}"; shift
exec python3 -u "$script" "$@"
