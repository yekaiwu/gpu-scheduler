#!/usr/bin/env python3
"""Reference cooperative worker (stdlib only). Declares `checkpoint: cooperative`.

Contract (docs/implementation/08):
  * on start, read GSCHED_CHECKPOINT_DIR; if GSCHED_RESUME == "1", resume from the saved state
  * on SIGUSR1: finish the current step, write the state, write READY last (fsync), exit 0
  * at the end of the work, print "done" and exit 0 WITHOUT writing READY
"""

import argparse
import json
import os
import signal
import sys
import time

stop_requested = False


def on_usr1(signum, frame):
    global stop_requested
    stop_requested = True


def write_state(ckpt_dir, i):
    tmp = os.path.join(ckpt_dir, "state.json.tmp")
    with open(tmp, "w") as fh:
        json.dump({"i": i}, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, os.path.join(ckpt_dir, "state.json"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=600)
    ap.add_argument("--step-s", type=float, default=1.0)
    args = ap.parse_args()
    ckpt_dir = os.environ.get("GSCHED_CHECKPOINT_DIR") or "."
    os.makedirs(ckpt_dir, exist_ok=True)
    i = 0
    if os.environ.get("GSCHED_RESUME") == "1":
        try:
            with open(os.path.join(ckpt_dir, "state.json")) as fh:
                i = json.load(fh)["i"]
            print(f"resumed at {i}", flush=True)
        except (OSError, ValueError, KeyError):
            print("resume requested but no usable state; starting fresh", flush=True)
    signal.signal(signal.SIGUSR1, on_usr1)
    while i < args.steps:
        if stop_requested:
            write_state(ckpt_dir, i)
            with open(os.path.join(ckpt_dir, "READY"), "w") as fh:     # READY last
                fh.write("ready\n")
                fh.flush()
                os.fsync(fh.fileno())
            print(f"checkpointed at {i}", flush=True)
            sys.exit(0)
        i += 1
        time.sleep(args.step_s)
        print(f"step {i}", flush=True)
        if i % 10 == 0:
            write_state(ckpt_dir, i)
    print("done", flush=True)


if __name__ == "__main__":
    main()
