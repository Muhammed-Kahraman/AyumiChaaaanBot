#!/bin/bash
# AyumuChanBot launcher
set -e
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"
exec python3 bot.py
