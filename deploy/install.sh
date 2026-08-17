#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/prompt-lens}"
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip --no-cache-dir
.venv/bin/pip install --no-cache-dir -r requirements.txt
install -m 0644 deploy/prompt-lens.service /etc/systemd/system/prompt-lens.service
systemctl daemon-reload
systemctl enable --now prompt-lens.service
systemctl --no-pager --full status prompt-lens.service
