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
    REMAINING=$(.venv/bin/python -c "
from datetime import datetime
t = datetime.now().replace(hour=${TARGET_H#0}, minute=${TARGET_M#0}, second=0, microsecond=0)
print(int((t - datetime.now()).total_seconds()))
")
    echo "[$(date '+%H:%M:%S')] waiting... ${REMAINING}s remaining"
    sleep 60
done

echo ""
echo "================================================================"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚀 LAUNCHING run_weekly_all_tiers.py --top 25"
echo "================================================================"
echo ""

exec .venv/bin/python -u scripts/run_weekly_all_tiers.py --top 25 2>&1 | tee "$LOG"
