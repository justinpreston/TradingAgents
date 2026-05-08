#!/bin/bash
# Sleep until a target HH:MM, then exec run_weekly_all_tiers.py.
#
# Usage:
#   scripts/_kickoff_at.sh [HHMM]    # default: 1700
#
# Examples:
#   scripts/_kickoff_at.sh           # fires at 17:00 local
#   scripts/_kickoff_at.sh 1635      # fires at 16:35 local
set -e
cd /Users/jpp5q/Documents/GitHub/TradingAgents

TARGET="${1:-1700}"
if [[ ! "$TARGET" =~ ^[0-2][0-9][0-5][0-9]$ ]]; then
    echo "ERROR: target must be HHMM (e.g. 1635). Got: '$TARGET'" >&2
    exit 2
fi
TARGET_H="${TARGET:0:2}"
TARGET_M="${TARGET:2:2}"

LOG="runs/weekly_workflow_$(date +%Y%m%d_%H%M).log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Scheduled kickoff queued for ${TARGET_H}:${TARGET_M} local"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Log file: $LOG"

while [[ "$(date +%H%M)" < "$TARGET" ]]; do
    # Pure-shell remaining-seconds math via macOS `date -j -f`. Avoids the
    # Python 3.13 init_sys_streams crash that fires when running .venv/bin/python
    # in a subshell of a nohup'd parent (bad stdin fd inheritance).
    TARGET_EPOCH=$(date -j -f "%H:%M" "${TARGET_H}:${TARGET_M}" +%s 2>/dev/null || echo 0)
    NOW_EPOCH=$(date +%s)
    REMAINING=$((TARGET_EPOCH - NOW_EPOCH))
    echo "[$(date '+%H:%M:%S')] waiting... ${REMAINING}s remaining"
    sleep 60
done

echo ""
echo "================================================================"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚀 LAUNCHING run_weekly_all_tiers.py --top 25"
echo "================================================================"
echo ""

exec .venv/bin/python -u scripts/run_weekly_all_tiers.py --top 25 2>&1 | tee "$LOG"
