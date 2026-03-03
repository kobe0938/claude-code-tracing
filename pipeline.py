#!/usr/bin/env python3
"""
SWE-bench Pro pipeline: run tasks sequentially, collect and parse traces.

Usage:
    # Run tasks 1 through 5 (1-indexed), trail 1:
    python pipeline.py --start 1 --end 5 --trail 1

    # Run a single task:
    python pipeline.py --start 3 --end 3 --trail 1

    # Resume from task 10:
    python pipeline.py --start 10 --end 20 --trail 1
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = BASE_DIR / "workspace"
PROBLEM_FILE = BASE_DIR / "problem_statement.md"
RUN_AGENT_SCRIPT = BASE_DIR / "run_agent.py"

TRACE_SOURCE = Path("/Users/kobe/Desktop/lmcache-agent-trace/claudecode/traces.jsonl")
TRACE_RAW_DIR = Path("/Users/kobe/Desktop/claude-code-tracing/swe-bench-pro/trail_3/raw")
TRACE_PARSED_DIR = Path("/Users/kobe/Desktop/claude-code-tracing/swe-bench-pro/trail_3/parsed")
PARSE_SCRIPT = Path("/Users/kobe/Desktop/lmcache-agent-trace/claudecode/parse_traces_raw_request.py")

VENV_PYTHON = BASE_DIR / ".venv" / "bin" / "python"

_dataset_cache = None


def load_dataset_cached():
    global _dataset_cache
    if _dataset_cache is None:
        from datasets import load_dataset
        _dataset_cache = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
    return _dataset_cache


def collect_traces(task_id: int, trail: int):
    """Copy trace file, parse it, then clear the source."""
    filename = f"swe_pro_task_{task_id}_interactive_plan_yolo_trail_{trail}.jsonl"

    TRACE_RAW_DIR.mkdir(parents=True, exist_ok=True)
    TRACE_PARSED_DIR.mkdir(parents=True, exist_ok=True)

    raw_dest = TRACE_RAW_DIR / filename
    parsed_dest = TRACE_PARSED_DIR / filename

    # Copy raw trace
    shutil.copy2(TRACE_SOURCE, raw_dest)
    print(f"  Copied trace -> {raw_dest}")

    # Parse trace
    subprocess.run(
        [sys.executable, str(PARSE_SCRIPT), "--input", str(raw_dest), "--output", str(parsed_dest)],
        check=True,
    )
    print(f"  Parsed trace -> {parsed_dest}")

    # Clear source trace file
    TRACE_SOURCE.write_text("")
    print(f"  Cleared {TRACE_SOURCE}")


def setup_workspace(entry):
    """Clean workspace, clone repo, checkout base_commit, write problem statement."""
    repo = entry["repo"]
    base_commit = entry["base_commit"]
    repo_url = f"https://github.com/{repo}.git"

    # Clone
    print(f"  Cloning {repo_url} ...")
    subprocess.run(
        ["git", "clone", "--quiet", repo_url, str(WORKSPACE_DIR)],
        check=True,
    )

    # Checkout base commit
    print(f"  Checking out {base_commit[:12]}...")
    subprocess.run(
        ["git", "checkout", base_commit],
        cwd=WORKSPACE_DIR, check=True,
        capture_output=True, text=True,
    )

    # Write problem statement (JSON-decode the string to get real newlines)
    raw = entry["problem_statement"]
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        decoded = raw
    PROBLEM_FILE.write_text(decoded)
    print(f"  Problem statement saved ({len(decoded)} chars)")


def run_agent():
    """Run the agent via run_agent.py."""
    subprocess.run(
        [str(VENV_PYTHON), str(RUN_AGENT_SCRIPT),
         "--workdir", str(WORKSPACE_DIR),
         "--query-file", str(PROBLEM_FILE)],
        check=True,
    )


def main():
    import argparse
    parser = argparse.ArgumentParser(description="SWE-bench Pro pipeline")
    parser.add_argument("--start", type=int, required=True, help="Start task number (1-indexed)")
    parser.add_argument("--end", type=int, required=True, help="End task number (1-indexed, inclusive)")
    parser.add_argument("--trail", type=int, default=1, help="Trail number (default: 1)")
    args = parser.parse_args()

    ds = load_dataset_cached()
    total = len(ds)

    if args.start < 1 or args.end > total or args.start > args.end:
        print(f"Error: invalid range. Dataset has {total} tasks (1-{total}).")
        sys.exit(1)

    print(f"Pipeline: tasks {args.start}-{args.end}, trail {args.trail}")
    print(f"Dataset: {total} instances\n")

    for task_id in range(args.start, args.end + 1):
        idx = task_id - 1  # dataset is 0-indexed
        entry = ds[idx]

        print(f"{'='*60}")
        print(f"TASK {task_id}/{args.end} | {entry['instance_id']}")
        print(f"  repo: {entry['repo']} | lang: {entry['repo_language']}")
        print(f"{'='*60}")

        # Step 0: Clean slate — clear workspace and trace file
        print("\n[Clean]")
        if WORKSPACE_DIR.exists():
            shutil.rmtree(WORKSPACE_DIR)
            print(f"  Removed old workspace")
        TRACE_SOURCE.write_text("")
        print(f"  Cleared trace file")

        # Step 1: Setup workspace
        print("\n[Setup]")
        setup_workspace(entry)

        # Step 2: Run agent
        print("\n[Agent]")
        run_agent()

        # Step 3: Collect traces
        print("\n[Traces]")
        collect_traces(task_id, args.trail)

        # Step 4: Clean up workspace after task
        print("\n[Cleanup]")
        if WORKSPACE_DIR.exists():
            shutil.rmtree(WORKSPACE_DIR)
            print(f"  Removed workspace")

        print(f"\nTask {task_id} complete.\n")

    print(f"All done! Tasks {args.start}-{args.end} finished.")


if __name__ == "__main__":
    main()
