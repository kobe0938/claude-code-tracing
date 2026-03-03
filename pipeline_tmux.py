#!/usr/bin/env python3
"""
SWE-bench Pro pipeline using tmux for Claude Code automation.

Usage:
    python pipeline_tmux.py --start 1 --end 5 --trail 1
    python pipeline_tmux.py --start 3 --end 3 --trail 1
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = BASE_DIR / "workspace"
PROBLEM_FILE = BASE_DIR / "problem_statement.md"
CLAUDE_BIN = "/Users/kobe/.local/bin/claude"

TRACE_SOURCE = Path("/Users/kobe/Desktop/lmcache-agent-trace/claudecode/traces.jsonl")
TRACE_RAW_DIR = Path("/Users/kobe/Desktop/claude-code-tracing/swe-bench-pro/trail_3/raw")
TRACE_PARSED_DIR = Path("/Users/kobe/Desktop/claude-code-tracing/swe-bench-pro/trail_3/parsed")
PARSE_SCRIPT = Path("/Users/kobe/Desktop/lmcache-agent-trace/claudecode/parse_traces_raw_request.py")

TMUX_SESSION = "claude_agent"

# ── Dataset ────────────────────────────────────────────────────────────────────
_dataset_cache = None

def load_dataset_cached():
    global _dataset_cache
    if _dataset_cache is None:
        from datasets import load_dataset
        _dataset_cache = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
    return _dataset_cache

# ── tmux helpers ───────────────────────────────────────────────────────────────

def tmux_session_exists():
    r = subprocess.run(["tmux", "has-session", "-t", TMUX_SESSION],
                       capture_output=True)
    return r.returncode == 0


def kill_session():
    subprocess.run(["pkill", "-f", "claude.*dangerously"], capture_output=True)
    time.sleep(1)
    if tmux_session_exists():
        subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION],
                       capture_output=True)
        time.sleep(1)


def send_keys(keys, enter=False):
    cmd = ["tmux", "send-keys", "-t", TMUX_SESSION, keys]
    if enter:
        cmd.append("Enter")
    subprocess.run(cmd, capture_output=True)


def send_text_literal(text):
    """Send text literally (no key interpretation) using send-keys -l."""
    subprocess.run(
        ["tmux", "send-keys", "-t", TMUX_SESSION, "-l", text],
        capture_output=True,
    )


def capture_pane():
    """Capture the visible content of the tmux pane."""
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", TMUX_SESSION, "-p"],
        capture_output=True, text=True,
    )
    return r.stdout if r.returncode == 0 else ""


def claude_is_running():
    r = subprocess.run(["pgrep", "-f", "claude.*dangerously"],
                       capture_output=True, text=True)
    return r.returncode == 0


def get_trace_size():
    try:
        return TRACE_SOURCE.stat().st_size
    except FileNotFoundError:
        return 0

# ── Workspace setup ────────────────────────────────────────────────────────────

def setup_workspace(entry):
    repo = entry["repo"]
    base_commit = entry["base_commit"]
    repo_url = f"https://github.com/{repo}.git"

    print(f"  Cloning {repo_url} ...")
    subprocess.run(["git", "clone", "--quiet", repo_url, str(WORKSPACE_DIR)],
                   check=True)

    print(f"  Checking out {base_commit[:12]}...")
    subprocess.run(["git", "checkout", base_commit],
                   cwd=WORKSPACE_DIR, check=True,
                   capture_output=True, text=True)

    raw = entry["problem_statement"]
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        decoded = raw
    PROBLEM_FILE.write_text(decoded)
    print(f"  Problem statement saved ({len(decoded)} chars)")

# ── Trace collection ──────────────────────────────────────────────────────────

def collect_traces(task_id: int, trail: int):
    filename = f"swe_pro_task_{task_id}_interactive_plan_yolo_trail_{trail}.jsonl"
    TRACE_RAW_DIR.mkdir(parents=True, exist_ok=True)
    TRACE_PARSED_DIR.mkdir(parents=True, exist_ok=True)

    raw_dest = TRACE_RAW_DIR / filename
    parsed_dest = TRACE_PARSED_DIR / filename

    shutil.copy2(TRACE_SOURCE, raw_dest)
    print(f"  Copied trace -> {raw_dest}")

    subprocess.run(
        [sys.executable, str(PARSE_SCRIPT),
         "--input", str(raw_dest), "--output", str(parsed_dest)],
        check=True,
    )
    print(f"  Parsed trace -> {parsed_dest}")

    TRACE_SOURCE.write_text("")
    print(f"  Cleared {TRACE_SOURCE}")

# ── Claude Code automation ─────────────────────────────────────────────────────

def launch_claude(workdir, max_retries=3):
    for attempt in range(1, max_retries + 1):
        print(f"    Attempt {attempt}/{max_retries}")
        kill_session()
        time.sleep(2)

        env_str = (
            f"TERM=xterm-256color "
            f"ANTHROPIC_BASE_URL=http://localhost:4000 "
            f'ANTHROPIC_MODEL=claude-sonnet-4-6 '
            f'ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer sk-Qjll6dkY-JOVIW882cK41w"'
        )

        # tmux new-session runs the command directly — no shell startup race
        subprocess.run([
            "tmux", "new-session", "-d", "-s", TMUX_SESSION,
            "-x", "200", "-y", "50",
            f"cd {workdir} && env {env_str} {CLAUDE_BIN} --dangerously-skip-permissions",
        ], check=True)

        started = False
        for _ in range(30):
            time.sleep(1)
            if claude_is_running():
                started = True
                break

        if not started:
            print(f"    Claude didn't start")
            continue

        alive = True
        for _ in range(5):
            time.sleep(2)
            if not claude_is_running():
                alive = False
                break

        if not alive:
            print(f"    Claude started but exited")
            continue

        print("    Claude Code is running and stable.")
        return True

    return False


def wait_trace_done(timeout=1800, poll=10, required_stable=3, min_wait=60):
    start = time.time()
    prev_size = get_trace_size()
    stable_count = 0
    first_growth = False

    while time.time() - start < timeout:
        time.sleep(poll)
        elapsed = time.time() - start

        if not claude_is_running():
            print(f"    Claude exited after {int(elapsed)}s (trace: {get_trace_size()} bytes)")
            return True

        size = get_trace_size()

        if size > prev_size:
            first_growth = True
            stable_count = 0
            prev_size = size
        elif first_growth:
            stable_count += 1
            if stable_count >= required_stable and elapsed >= min_wait:
                print(f"    Done after {int(elapsed)}s (trace: {size} bytes)")
                return True

        if not first_growth and elapsed > 300:
            if claude_is_running():
                print(f"    Warning: no trace activity after {int(elapsed)}s but Claude still running, continuing...")
            else:
                print(f"    Claude exited with no trace activity after {int(elapsed)}s")
                return False

    raise TimeoutError(f"Not done after {timeout}s")


def send_query(query, chunk_size=200, chunk_delay=0.3):
    single_line = query.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    for i in range(0, len(single_line), chunk_size):
        send_text_literal(single_line[i:i + chunk_size])
        time.sleep(chunk_delay)


def is_still_processing(pane_content):
    """Check if Claude Code TUI shows active processing indicators."""
    lines = pane_content.strip().split('\n')
    bottom = '\n'.join(lines[-20:]) if len(lines) > 20 else pane_content
    lower = bottom.lower()

    if any(kw in lower for kw in ['thinking', 'crunching', 'brewing']):
        return True

    spinner_chars = set('⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏')
    if any(c in spinner_chars for c in bottom):
        return True

    return False


def menu_visible(pane_content=None):
    """Check if Claude Code's completion menu is visible.

    Looks for the actual completion prompt ('Would you like to proceed?')
    or the ❯ selector on a numbered option line (e.g. '❯ 1. Yes, ...').
    """
    if pane_content is None:
        pane_content = capture_pane()
    lines = pane_content.strip().split("\n")
    bottom = lines[-15:] if len(lines) > 15 else lines
    bottom_text = "\n".join(bottom)

    if "would you like to proceed" in bottom_text.lower():
        return True

    for line in bottom:
        stripped = line.strip()
        if not stripped.startswith('❯'):
            continue
        rest = stripped[1:].strip()
        if rest and rest[0].isdigit():
            return True

    return False


def wait_for_menu(timeout=1800, poll=5, required_consecutive=3):
    """Wait until the menu persists at the bottom AND trace has stopped growing
    AND Claude is not actively processing (thinking/crunching).
    All three conditions must hold for multiple consecutive checks."""
    start = time.time()
    consecutive = 0
    prev_trace = get_trace_size()

    while time.time() - start < timeout:
        if not claude_is_running():
            print("    Claude exited while waiting for menu")
            return False

        pane = capture_pane()
        trace_now = get_trace_size()
        trace_stable = (trace_now == prev_trace)
        prev_trace = trace_now

        processing = is_still_processing(pane)
        has_menu = menu_visible(pane)

        if has_menu and trace_stable and not processing:
            consecutive += 1
            if consecutive >= required_consecutive:
                return True
        else:
            consecutive = 0

        elapsed = int(time.time() - start)
        if elapsed > 0 and elapsed % 60 < poll:
            trace_mb = trace_now / 1_000_000
            print(f"    Still waiting... ({elapsed}s, trace: {trace_mb:.1f}MB, stable: {trace_stable}, processing: {processing})")
        time.sleep(poll)
    return False


def select_second_option(max_attempts=10):
    """Select the second option from Claude's menu with retry."""
    time.sleep(1)

    # Move to second option once
    send_keys("Down")
    time.sleep(1)

    baseline_size = get_trace_size()
    min_growth = 10000

    for attempt in range(1, max_attempts + 1):
        print(f"    Enter attempt {attempt}/{max_attempts}")
        send_keys("Enter")
        time.sleep(5)

        if not claude_is_running():
            print("    Claude exited after selection")
            return True

        # If menu is gone from the bottom, selection worked even if trace hasn't grown much yet
        if not menu_visible():
            print("    Menu dismissed — selection accepted")
            return True

        growth = get_trace_size() - baseline_size
        if growth >= min_growth:
            print(f"    Execution started (trace grew {growth} bytes)")
            return True

        print(f"    Menu still visible, retrying Enter...")

    print(f"    WARNING: selection may not have worked after {max_attempts} attempts")
    return False


def run_agent(workdir, query, timeout=1800):
    """Full agent lifecycle: launch -> plan -> select -> execute -> exit."""
    print(f"[1/7] Launching Claude Code in {workdir}")
    if not launch_claude(workdir):
        print("    FAILED: Could not launch Claude Code after retries.")
        kill_session()
        return False

    print("[2/7] Entering plan mode")
    time.sleep(2)
    send_text_literal("/plan")
    time.sleep(1)
    send_keys("Enter")
    time.sleep(3)

    print(f"[3/7] Sending query ({len(query)} chars)")
    send_query(query)
    time.sleep(2)
    send_keys("Enter")
    time.sleep(3)

    print("[4/7] Waiting for plan to complete...")
    if wait_for_menu(timeout=timeout):
        print("    Plan complete — options menu appeared")
    else:
        print("    WARNING: menu not detected, trying to proceed anyway...")

    print("[5/7] Selecting second option")
    select_second_option()

    print("[6/7] Waiting for execution to complete...")
    wait_trace_done(timeout=timeout, min_wait=60, required_stable=18)

    print("[7/7] Exiting Claude Code")
    send_text_literal("/exit")
    time.sleep(1)
    send_keys("Enter")
    time.sleep(3)
    kill_session()
    print("    Session ended.")
    return True

# ── Main pipeline ──────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="SWE-bench Pro pipeline (tmux)")
    parser.add_argument("--start", type=int, required=True, help="Start task (1-indexed)")
    parser.add_argument("--end", type=int, required=True, help="End task (1-indexed, inclusive)")
    parser.add_argument("--trail", type=int, default=1, help="Trail number")
    parser.add_argument("--timeout", type=int, default=1800, help="Per-phase timeout in seconds")
    args = parser.parse_args()

    ds = load_dataset_cached()
    total = len(ds)

    if args.start < 1 or args.end > total or args.start > args.end:
        print(f"Error: invalid range. Dataset has {total} tasks (1-{total}).")
        sys.exit(1)

    print(f"Pipeline: tasks {args.start}-{args.end}, trail {args.trail}")
    print(f"Dataset: {total} instances\n")

    for task_id in range(args.start, args.end + 1):
        idx = task_id - 1
        entry = ds[idx]

        print(f"{'='*60}")
        print(f"TASK {task_id}/{args.end} | {entry['instance_id']}")
        print(f"  repo: {entry['repo']} | lang: {entry['repo_language']}")
        print(f"{'='*60}")

        print("\n[Clean]")
        if WORKSPACE_DIR.exists():
            shutil.rmtree(WORKSPACE_DIR)
            print("  Removed old workspace")
        TRACE_SOURCE.write_text("")
        print("  Cleared trace file")

        print("\n[Setup]")
        setup_workspace(entry)

        print("\n[Agent]")
        query = PROBLEM_FILE.read_text().strip()
        success = run_agent(str(WORKSPACE_DIR.resolve()), query, timeout=args.timeout)

        if not success:
            print(f"\n  Task {task_id} FAILED — skipping trace collection.\n")
            continue

        print("\n[Traces]")
        collect_traces(task_id, args.trail)

        print("\n[Cleanup]")
        if WORKSPACE_DIR.exists():
            shutil.rmtree(WORKSPACE_DIR)
            print("  Removed workspace")

        print(f"\nTask {task_id} complete.\n")

    print(f"All done! Tasks {args.start}-{args.end} finished.")


if __name__ == "__main__":
    main()
