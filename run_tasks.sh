#!/bin/bash
cd /Users/kobe/Desktop/swe-bench-pro-claude-code
source .venv/bin/activate

for i in 48 49 50; do
    echo "========== Starting task $i =========="
    python pipeline_tmux.py --start "$i" --end "$i" --trail 1 || echo "Task $i failed, skipping..."
done

echo "All tasks attempted."
