#!/usr/bin/env bash
#
# launch-mobile-qa.sh — thin wrapper around launch-mobile-qa.py.
#
# Full device emulation lives in the Python launcher because a feedback form reports its
# technical info from User-Agent Client Hints, which can only be overridden through the
# DevTools Protocol. Chrome's --user-agent flag rewrites the UA string alone, which leaves the
# Inbox recording "Mobile Device: No" and the real desktop Chrome version.
#
# USAGE
#   ./launch-mobile-qa.sh [ios|android|tablet] [url]
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/launch-mobile-qa.py" "$@"
