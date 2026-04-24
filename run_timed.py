#!/usr/bin/env python3
"""Timed wrapper around eAudit's `ecapd` collector.

Starts `./ecapd` and stops it cleanly (SIGINT -> flush ring buffer) after a
duration or at a wall-clock time.

Examples:
    ./run_timed.py -d 30m -o /tmp/eaudit.cap
    ./run_timed.py -d 1h30m -o captures/run1.cap
    ./run_timed.py -u '2026-04-19 22:00 UTC' -o /tmp/eaudit.cap
    ./run_timed.py -d 5m -o /tmp/eaudit.cap -- -v3 -b 4 -r 16
    # args after `--` are passed straight to ecapd.

Must be run with sudo (ecapd requires root). If not root, the script
re-execs itself under sudo.
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone


DURATION_RE = re.compile(
    r'^\s*(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?!s))?\s*(?:(\d+)\s*s)?\s*$',
    re.I)


def parse_duration(s: str) -> float:
    """Accept 1h, 30m, 90s, 1d2h, 1h30m, or a bare number of seconds."""
    if s.isdigit():
        return float(s)
    m = DURATION_RE.match(s)
    if not m or not any(m.groups()):
        raise argparse.ArgumentTypeError(
            f'bad duration {s!r} (expected e.g. 30m, 1h, 1h30m, 1d)')
    d, h, mi, se = (int(g or 0) for g in m.groups())
    total = d * 86400 + h * 3600 + mi * 60 + se
    if total <= 0:
        raise argparse.ArgumentTypeError(f'duration must be positive, got {s!r}')
    return float(total)


def parse_until(s: str) -> float:
    """Accept an absolute stop time. Examples:
        2026-04-19 22:00
        2026-04-19 22:00 UTC
        2026-04-19T22:00:00+00:00
        2026-04-19:22:00 UTC
    Returns epoch seconds. Without an explicit zone, assumes UTC.
    """
    raw = s.strip()
    # Tolerate `YYYY-MM-DD:HH:MM` by swapping the first separator.
    raw2 = re.sub(r'^(\d{4}-\d{2}-\d{2}):', r'\1 ', raw)
    # Strip a trailing 'UTC' / 'Z' — treat both as explicit UTC.
    tz = timezone.utc
    raw2 = re.sub(r'\s*UTC\s*$', '', raw2, flags=re.I).strip()
    raw2 = re.sub(r'Z$', '', raw2)
    try:
        dt = datetime.fromisoformat(raw2)
    except ValueError:
        # Try a handful of common layouts.
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M',
                    '%Y/%m/%d %H:%M:%S', '%Y/%m/%d %H:%M'):
            try:
                dt = datetime.strptime(raw2, fmt)
                break
            except ValueError:
                continue
        else:
            raise argparse.ArgumentTypeError(f'bad --until {s!r}')
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    ts = dt.timestamp()
    if ts <= time.time():
        raise argparse.ArgumentTypeError(
            f'--until {s!r} resolves to {dt.isoformat()} — in the past')
    return ts


def fmt_secs(sec: float) -> str:
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d: parts.append(f'{d}d')
    if h: parts.append(f'{h}h')
    if m: parts.append(f'{m}m')
    if s or not parts: parts.append(f'{s}s')
    return ''.join(parts)


def main():
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__)
    when = ap.add_mutually_exclusive_group(required=True)
    when.add_argument('-d', '--duration', type=parse_duration,
                      help='how long to capture (e.g. 30m, 1h, 1h30m, 1d, 90s)')
    when.add_argument('-u', '--until', type=parse_until,
                      help="absolute stop time, UTC default "
                           "(e.g. '2026-04-19 22:00', '2026-04-19T22:00Z')")
    ap.add_argument('-o', '--output', required=True,
                    help='capture file path passed to ecapd as -c')
    ap.add_argument('--ecapd', default=None,
                    help='path to ecapd (default: ./ecapd next to this script)')
    ap.add_argument('-g', '--grace', type=float, default=60.0,
                    help='seconds to wait for ecapd to flush + tear down BCC '
                         'after SIGINT (default 60; BCC kprobe detach can '
                         'take 10-30s on kernel 6.x)')
    ap.add_argument('extra', nargs='*',
                    help='extra args passed verbatim to ecapd (put after `--`)')
    args = ap.parse_args()

    # Re-exec under sudo if not root. Preserve all argv.
    if os.geteuid() != 0:
        print('[run_timed] not root, re-execing under sudo', file=sys.stderr)
        os.execvp('sudo', ['sudo', '-E', sys.executable, os.path.abspath(__file__),
                           *sys.argv[1:]])

    ecapd = args.ecapd or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'ecapd')
    if not os.path.isfile(ecapd) or not os.access(ecapd, os.X_OK):
        sys.exit(f'[run_timed] ecapd not found or not executable: {ecapd}')

    # Resolve stop time and duration for logging
    now = time.time()
    if args.duration is not None:
        stop_at = now + args.duration
        remaining = args.duration
    else:
        stop_at = args.until
        remaining = stop_at - now

    # Make sure the output directory exists
    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)

    stop_iso = datetime.fromtimestamp(stop_at, tz=timezone.utc).isoformat()
    cmd = [ecapd, '-c', args.output, *args.extra]
    print(f'[run_timed] starting: {" ".join(cmd)}', file=sys.stderr)
    print(f'[run_timed] will stop at {stop_iso} (in {fmt_secs(remaining)})',
          file=sys.stderr)

    # Run in its own process group so we can signal the whole tree.
    proc = subprocess.Popen(cmd, preexec_fn=os.setsid)

    def forward(signum, _frame):
        print(f'\n[run_timed] got signal {signum}, stopping ecapd early',
              file=sys.stderr)
        try:
            os.killpg(proc.pid, signal.SIGINT)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGINT, forward)
    signal.signal(signal.SIGTERM, forward)

    # Sleep until stop_at, but wake up early if ecapd dies on its own.
    try:
        while True:
            left = stop_at - time.time()
            if left <= 0:
                break
            try:
                rc = proc.wait(timeout=min(left, 5.0))
                print(f'[run_timed] ecapd exited on its own with rc={rc}',
                      file=sys.stderr)
                sys.exit(rc or 0)
            except subprocess.TimeoutExpired:
                continue
    except KeyboardInterrupt:
        pass

    # Time's up — send SIGINT to the group (ecapd wraps python, which catches it).
    print('[run_timed] time elapsed, sending SIGINT', file=sys.stderr)
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except ProcessLookupError:
        pass

    # Poll with a progress tick every 10s. BCC teardown (detaching kprobes)
    # runs *after* the ring buffer flush and capture file are already safe,
    # so patience here doesn't risk data loss.
    t0 = time.time()
    rc = None
    while True:
        try:
            rc = proc.wait(timeout=5.0)
            break
        except subprocess.TimeoutExpired:
            elapsed = time.time() - t0
            if elapsed >= args.grace:
                break
            print(f'[run_timed] waiting for ecapd to finish teardown '
                  f'({elapsed:.0f}/{args.grace:.0f}s — capture is already '
                  f'safely written)', file=sys.stderr)

    if rc is None:
        print(f'[run_timed] grace of {args.grace}s exceeded; capture file is '
              f'valid but forcing exit (SIGTERM)', file=sys.stderr)
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print('[run_timed] still alive, SIGKILL '
                  '(capture itself remains valid; only teardown was aborted)',
                  file=sys.stderr)
            os.killpg(proc.pid, signal.SIGKILL)
            rc = proc.wait()

    try:
        size = os.path.getsize(args.output)
        print(f'[run_timed] wrote {size} bytes to {args.output} (ecapd rc={rc})',
              file=sys.stderr)
        # Capture is valid; report success regardless of how teardown exited.
        sys.exit(0 if size > 0 else (rc or 0))
    except OSError:
        print(f'[run_timed] ecapd exited rc={rc}; no capture file at '
              f'{args.output}', file=sys.stderr)
        sys.exit(rc or 1)


if __name__ == '__main__':
    main()
