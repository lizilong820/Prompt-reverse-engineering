#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/prompt-lens}"
cd "$APP_DIR"
if command -v dnf >/dev/null 2>&1; then
    dnf install -y mesa-libgbm
elif command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y libgbm1
fi
python3 -m venv .venv
.venv/bin/pip install --upgrade pip --no-cache-dir
.venv/bin/pip install --no-cache-dir -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/.playwright-browsers" .venv/bin/python -m playwright install chromium
install -m 0644 deploy/prompt-lens.service /etc/systemd/system/prompt-lens.service
systemctl daemon-reload
systemctl enable --now prompt-lens.service
systemctl --no-pager --full status prompt-lens.service
