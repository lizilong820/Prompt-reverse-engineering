#!/usr/bin/env bash
set -euo pipefail

if command -v nginx >/dev/null 2>&1; then
    nginx -t
    nginx -s reload
fi
