#!/bin/sh
set -eu
/app/scripts/download-model.sh "${DEFAULT_MODEL:-medium.en}"
exec /app/.venv/bin/python /app/src/main.py
