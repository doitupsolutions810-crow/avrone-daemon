#!/bin/sh
# Run the fusion daemon contract tests and write the evidence JSON.
# Docker is not required. This does not start compose and does not push.
cd "$(dirname "$0")/.." || exit 1
exec python3 scripts/verify_fusion_daemon.py
