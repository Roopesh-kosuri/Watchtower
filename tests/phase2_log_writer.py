"""
Phase 2 test writer. Runs entirely independently of the app -- it just
writes real files with real timing, deliberately exercising:
  - the backend not being up yet / the log file not existing yet at boot
  - a normal steady stream of lines
  - an unterminated trailing line right before rotation
  - rename-based rotation (logrotate 'create' style)
  - copytruncate-style rotation (same inode, size resets)
  - a malformed line mixed into an otherwise well-formed JSON-lines stream

Prints a JSON summary of exactly how many lines it wrote to each target,
so the test can assert DB counts match exactly.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "phase2_logs")
PLAINTEXT_PATH = os.path.join(BASE, "plaintext.log")
JSON_PATH = os.path.join(BASE, "json.log")

LINES_PER_SEGMENT = 15
LINE_DELAY = 0.15


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def write_plaintext() -> dict:
    count = {"pre_rotation": 0, "partial_trailing": 0, "post_rotation": 0}

    with open(PLAINTEXT_PATH, "a") as f:
        for i in range(LINES_PER_SEGMENT):
            f.write(f"{ts()} [INFO] plaintext pre-rotation line {i}\n")
            f.flush()
            count["pre_rotation"] += 1
            time.sleep(LINE_DELAY)

        # Deliberately no trailing newline -- tests that the watcher flushes
        # a partial buffered line instead of dropping it on rotation.
        f.write(f"{ts()} [WARN] plaintext trailing partial line no newline")
        f.flush()
        count["partial_trailing"] += 1

    time.sleep(0.5)  # let the watcher catch up before we pull the rug

    # Rename-based rotation: path momentarily has nothing at it until the
    # next open("a") recreates it -- exercises the FileNotFoundError retry
    # path in the watcher too.
    os.rename(PLAINTEXT_PATH, PLAINTEXT_PATH + ".1")
    time.sleep(0.3)

    with open(PLAINTEXT_PATH, "a") as f:
        for i in range(LINES_PER_SEGMENT):
            f.write(f"{ts()} [INFO] plaintext post-rotation line {i}\n")
            f.flush()
            count["post_rotation"] += 1
            time.sleep(LINE_DELAY)

    return count


def write_json() -> dict:
    count = {"pre_truncate": 0, "post_truncate": 0, "malformed": 0}

    with open(JSON_PATH, "a") as f:
        for i in range(LINES_PER_SEGMENT):
            line = json.dumps(
                {"timestamp": ts(), "level": "INFO", "message": f"json pre-truncate line {i}"}
            )
            f.write(line + "\n")
            f.flush()
            count["pre_truncate"] += 1
            time.sleep(LINE_DELAY)

    time.sleep(0.5)

    # Copytruncate-style rotation: same inode, size drops to 0.
    with open(JSON_PATH, "w"):
        pass

    with open(JSON_PATH, "a") as f:
        for i in range(LINES_PER_SEGMENT):
            line = json.dumps(
                {"timestamp": ts(), "level": "WARN", "message": f"json post-truncate line {i}"}
            )
            f.write(line + "\n")
            f.flush()
            count["post_truncate"] += 1
            time.sleep(LINE_DELAY)

        f.write("{this is not valid json\n")
        f.flush()
        count["malformed"] += 1

    return count


if __name__ == "__main__":
    os.makedirs(BASE, exist_ok=True)
    # Deliberately do NOT pre-create the files -- the backend should already
    # be running and polling for a path that doesn't exist yet.

    results: dict = {}

    t1 = threading.Thread(target=lambda: results.update(plaintext=write_plaintext()))
    t2 = threading.Thread(target=lambda: results.update(json=write_json()))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    print(json.dumps(results, indent=2))
