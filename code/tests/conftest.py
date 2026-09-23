"""Shared pytest setup: put ``code/`` on the import path.

``code/`` itself is never added as a package name because ``code`` is a stdlib
module; instead its contents (``formatting``, ``evaluation``) import directly.
No API key is read here or in any test.
"""

import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CODE_DIR.parent

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))
