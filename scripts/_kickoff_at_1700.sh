#!/bin/bash
set -e
cd /Users/jpp5q/Documents/GitHub/TradingAgents
LOG="runs/weekly_workflow_$(date +%Y%m%d_%H%M).log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Scheduled kickoff queued for 17:00 local"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Log file: $LOG"

while [[ "$(date +%H%M)" < "1700" ]]; do
    REMAINING=$(.venv/bin/python -c "from datetime import datetime; t=datetime.now().replace(hour=17,minute=0,second=0,microsecond=0); print(int((t-datetime.now()).total_seconds()))")
    echo "[$(date '+%H:%M:%S')] waiting... ${REMAINING}s remaining"
    sleep 60
done

echo ""
echo "================================================================"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚀 LAUNCHING run_weekly_all_tiers.py --top 25"
echo "================================================================"
echo ""

exec .venv/bin/python -u scripts/run_weekly_all_tiers.py --top 25 2>&1 | tee "$LOG"
