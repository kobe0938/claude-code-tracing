#!/usr/bin/env python3
"""
Automate a Claude Code session: plan -> execute -> exit.
Uses GNU screen. Retries launch up to 3 times.
"""

import os
import subprocess
import time
import sys
import argparse
from pathlib import Path

SESSION = "claude_agent"
CLAUDE_BIN = "/Users/kobe/.local/bin/claude"
TRACE_FILE = Path("/Users/kobe/Desktop/lmcache-agent-trace/claudecode/traces.jsonl")


def send(text):
    subprocess.run(["screen", "-S", SESSION, "-X", "stuff", text], check=True)


def kill_session():
    subprocess.run(["pkill", "-f", "claude.*dangerously"], capture_output=True)
    time.sleep(1)
    for _ in range(10):
        result = subprocess.run(["screen", "-ls"], capture_output=True, text=True)
        if SESSION not in result.stdout:
            return
        subprocess.run(["screen", "-S", SESSION, "-X", "quit"], capture_output=True)
        time.sleep(1)
    subprocess.run(["screen", "-wipe"], capture_output=True)
    time.sleep(1)


def claude_is_running():
    result = subprocess.run(["pgrep", "-f", "claude.*dangerously"], capture_output=True, text=True)
    return result.returncode == 0


def screen_dump():
    tmp = Path("/tmp/claude_screen_dump.txt")
    tmp.unlink(missing_ok=True)
    subprocess.run(["screen", "-S", SESSION, "-X", "hardcopy", str(tmp)], capture_output=True)
    time.sleep(0.3)
    if tmp.exists():
        content = tmp.read_bytes().decode("utf-8", errors="replace")
        lines = [l for l in content.split("\n") if l.strip()]
        return "\n".join(lines)
    return "(empty)"


def get_trace_size():
    try:
        return TRACE_FILE.stat().st_size
    except FileNotFoundError:
        return 0


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
        send(single_line[i:i + chunk_size])
        time.sleep(chunk_delay)


def launch_claude(workdir, max_retries=3):
    launch_script = Path("/tmp/claude_launch.sh")

    for attempt in range(1, max_retries + 1):
        print(f"    Attempt {attempt}/{max_retries}")
        kill_session()
        time.sleep(3)

        # Write a self-contained launch script — no race conditions
        launch_script.write_text(f"""#!/bin/zsh
cd {workdir}
export TERM=xterm-256color
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_MODEL="claude-sonnet-4-6"
export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer sk-Qjll6dkY-JOVIW882cK41w"
exec {CLAUDE_BIN} --dangerously-skip-permissions
""")
        launch_script.chmod(0o755)

        env = os.environ.copy()
        env["SHELL_SESSIONS_DISABLE"] = "1"
        # -fn disables flow control so screen won't freeze Claude when no terminal is attached
        subprocess.run(
            ["screen", "-dmS", SESSION, "-fn", "/bin/zsh", str(launch_script)],
            check=True, env=env,
        )
        # nonblock prevents screen from blocking if output buffer fills up while detached
        subprocess.run(
            ["screen", "-S", SESSION, "-X", "nonblock", "on"],
            capture_output=True,
        )

        started = False
        for _ in range(30):
            time.sleep(1)
            if claude_is_running():
                started = True
                break

        if not started:
            dump = screen_dump()
            print(f"    Claude didn't start. Screen shows:\n{dump}")
            continue

        alive = True
        for _ in range(5):
            time.sleep(2)
            if not claude_is_running():
                alive = False
                break

        if not alive:
            dump = screen_dump()
            print(f"    Claude started but exited. Screen shows:\n{dump}")
            continue

        print("    Claude Code is running and stable.")
        return True

    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--query-file", required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    workdir = str(Path(args.workdir).resolve())
    query = Path(args.query_file).read_text().strip()

    print(f"[1/7] Launching Claude Code in {workdir}")
    if not launch_claude(workdir):
        print("    FAILED: Could not launch Claude Code after retries. Aborting.")
        kill_session()
        sys.exit(1)

    print("[3/7] Entering plan mode")
    time.sleep(2)
    send("/plan")
    time.sleep(1)
    send("\r")
    time.sleep(3)

    print(f"[4/7] Sending query ({len(query)} chars)")
    send_query(query)
    time.sleep(2)
    send("\r")
    time.sleep(3)

    print("[5/7] Waiting for plan to complete...")
    wait_trace_done(timeout=args.timeout, min_wait=60, required_stable=6)

    print("[6/7] Selecting second option")
    # Let residual trace writes finish and TUI render the options menu
    time.sleep(5)
    baseline_size = get_trace_size()
    min_growth = 10000  # require 10KB+ growth to confirm execution started

    for select_attempt in range(1, 8):
        print(f"    Selection attempt {select_attempt}/7 (baseline: {baseline_size})")
        send("\033[B")
        time.sleep(1)
        send("\r")
        time.sleep(8)

        if not claude_is_running():
            print("    Claude exited after selection")
            break

        current_size = get_trace_size()
        growth = current_size - baseline_size
        if growth >= min_growth:
            print(f"    Execution started (trace grew {growth} bytes)")
            break

        print(f"    Trace grew only {growth} bytes, selection likely didn't register. Retrying...")
    else:
        print("    WARNING: Selection may not have worked after 7 attempts")

    print("[7/7] Waiting for execution to complete...")
    wait_trace_done(timeout=args.timeout, min_wait=60, required_stable=18)

    print("[Done] Sending /exit")
    send("/exit")
    time.sleep(2)
    send("\r")
    time.sleep(2)

    kill_session()
    print("Session ended.")


if __name__ == "__main__":
    main()
