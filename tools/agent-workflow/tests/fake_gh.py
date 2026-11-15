"""Test double of the `gh` CLI (labelled; T-022 tests only): answers `repo view --json visibility` from the
environment, so the real child-process path of ci_window.py is exercised without network.

FAKE_GH_VISIBILITY: the visibility to print (default PRIVATE). FAKE_GH_SLEEP: seconds to sleep first (timeouts).
FAKE_GH_LOG: a file each call's argv is appended to, one JSON list per line.
"""

from __future__ import annotations

import json
import os
import sys
import time

if os.environ.get("FAKE_GH_LOG"):
    with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(sys.argv[1:]) + "\n")
time.sleep(float(os.environ.get("FAKE_GH_SLEEP", "0")))
if sys.argv[1:3] == ["repo", "view"]:
    print(json.dumps({"visibility": os.environ.get("FAKE_GH_VISIBILITY", "PRIVATE")}))
    sys.exit(0)
sys.stderr.write(f"fake_gh: unsupported {sys.argv[1:]}\n")
sys.exit(1)
