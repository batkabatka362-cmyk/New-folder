"""Autonomy supervisor — keep the PAPER bot alive whenever the machine is on.

Runs `python -m memebot` as a child and RESTARTS it whenever it exits unexpectedly (an unhandled
exception, a network-failure cascade, an OS hiccup), so "if my computer is on, the AI runs by itself"
holds without you babysitting it. Guards against a config-error crash-LOOP with exponential backoff +
a fast-crash cap (it gives up rather than spinning the CPU). A HEALTHY run resets the backoff.

Control:
  - STOP everything: Ctrl-C the supervisor (it terminates the child and exits), or create a `memebot.stop`
    file next to the DB (checked between restarts) for a graceful stop without killing.
  - A clean child exit (code 0) is treated as an intentional stop and is NOT restarted.
The bot's own single-instance lock prevents any overlap; the supervisor always waits for the child to
fully exit (releasing the lock) before relaunching.

  python -m memebot.supervisor            # the autonomy entrypoint (use instead of `python -m memebot`)

To run automatically at login (true "whenever the computer is on"), add this command to Windows Task
Scheduler / a startup shortcut — a deliberate, user-side OS step this script does NOT take for you.

PAPER-ONLY: this only keeps the existing paper bot running; it changes NOTHING about execution mode.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time


def _next_backoff(uptime_s: float, current: float, *, min_uptime: float, base: float, cap: float) -> float:
    """A child that ran at least `min_uptime` was HEALTHY -> reset to `base`; a fast crash -> double the
    backoff (capped). Pure, so the restart cadence is testable."""
    if uptime_s >= min_uptime:
        return base
    return min(cap, current * 2.0)


def _should_give_up(consecutive_fast_crashes: int, max_fast: int) -> bool:
    """Stop relaunching after this many back-to-back fast crashes (a deterministic config error keeps
    crashing instantly — relaunching forever would just burn CPU). max_fast <= 0 = never give up."""
    return max_fast > 0 and consecutive_fast_crashes >= max_fast


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def run(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Keep `python -m memebot` alive, restarting it on crashes.")
    ap.add_argument("--min-uptime", type=float, default=60.0, help="a run shorter than this counts as a fast crash")
    ap.add_argument("--base-backoff", type=float, default=3.0, help="restart delay after a healthy run (s)")
    ap.add_argument("--max-backoff", type=float, default=300.0, help="cap on the exponential backoff (s)")
    ap.add_argument("--max-fast-crashes", type=int, default=8, help="give up after this many back-to-back fast crashes (0=never)")
    ap.add_argument("--stop-file", default="memebot.stop", help="graceful-stop sentinel: present -> exit between restarts")
    args = ap.parse_args(argv)

    cmd = [sys.executable, "-u", "-m", "memebot"]
    backoff = args.base_backoff
    fast_crashes = 0
    starts = 0
    log = lambda m: print(f"[supervisor] {m}", flush=True)  # noqa: E731

    log(f"starting; will keep `{' '.join(cmd[2:])}` alive (Ctrl-C to stop everything)")
    while True:
        if os.path.exists(args.stop_file):
            log(f"stop-file '{args.stop_file}' present -> graceful stop")
            return 0
        starts += 1
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(cmd)              # inherits stdout/stderr -> the bot's logs flow through
        except Exception as e:  # noqa: BLE001
            log(f"failed to launch the bot: {type(e).__name__}: {e}")
            return 2
        try:
            code = proc.wait()
        except KeyboardInterrupt:
            log("Ctrl-C -> terminating the bot and exiting")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()
            return 0
        uptime = time.monotonic() - t0

        if code == 0:
            log(f"bot exited cleanly (code 0) after {uptime:.0f}s -> intentional stop, not restarting")
            return 0
        if uptime >= args.min_uptime:
            fast_crashes = 0                          # a healthy run clears the fast-crash streak
        else:
            fast_crashes += 1
        backoff = _next_backoff(uptime, backoff, min_uptime=args.min_uptime,
                                base=args.base_backoff, cap=args.max_backoff)
        log(f"bot exited code {code} after {uptime:.0f}s (run #{starts}, "
            f"fast-crash streak {fast_crashes}) -> restarting in {backoff:.0f}s")
        if _should_give_up(fast_crashes, args.max_fast_crashes):
            log(f"GIVING UP after {fast_crashes} back-to-back fast crashes — fix the error then relaunch the supervisor")
            return 3
        try:
            _sleep(backoff)
        except KeyboardInterrupt:
            log("Ctrl-C during backoff -> exiting")
            return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
