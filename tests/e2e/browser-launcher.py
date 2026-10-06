"""Register Playwright's detached browser session before replacing this launcher."""

import json
import os
from pathlib import Path
import sys


# Playwright creates a new POSIX session for executablePath. exec preserves its
# PID/group, so this record remains useful after an abrupt Node helper exit.
pid = os.getpid()
if os.getpgid(pid) != pid:
    raise SystemExit("Browser launcher must own its process group")
profile = next(arg.removeprefix("--user-data-dir=") for arg in sys.argv[1:]
               if arg.startswith("--user-data-dir="))
directory = Path(os.environ["NOMUSIC_VERIFY_BROWSER_OWNERSHIP"])
record = {"helper_pid": int(os.environ["NOMUSIC_VERIFY_BROWSER_HELPER"]),
          "pid": pid, "profile": profile}
staged = directory / f"{pid}.json.part"
staged.write_text(json.dumps(record))
staged.replace(directory / f"{pid}.json")
# If Node died before the handoff became visible, cleanup may already have
# scanned it. Never start Chromium after that scan: the parent must still own us.
if os.getppid() != record["helper_pid"]:
    raise SystemExit("Browser helper exited before its launch handoff")
executable = os.environ["NOMUSIC_VERIFY_CHROMIUM"]
os.execv(executable, [executable, *sys.argv[1:]])
