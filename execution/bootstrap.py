from __future__ import annotations

import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent

for entry in (str(REPO_ROOT), str(THIS_DIR)):
    if entry in sys.path:
        sys.path.remove(entry)
    sys.path.insert(0, entry)
