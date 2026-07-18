"""Locate the ``client`` package tree and put it on ``sys.path``.

The bridge REUSES the existing ``starmax_client`` library (transport + command builders +
dial codec) rather than reimplementing any of it. That library lives in the repo's ``client/``
directory and is not pip-installed, so we insert it on ``sys.path`` at import time. This lets
the bridge run straight from a checkout on the BLE host (``python -m gtx2_bridge …``) with no
install step. Tests get the same path via the root ``pytest.ini``'s ``pythonpath``; this
handles the runtime (CLI / ``serve``) case.
"""
from __future__ import annotations

import os
import sys

# bridge/gtx2_bridge/_paths.py -> repo root is two levels up from this file's dir.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
STARMAX_CLIENT_DIR = os.path.join(_REPO_ROOT, "client")


def ensure_starmax_on_path() -> None:
    """Insert ``<repo>/client`` on ``sys.path`` if ``starmax_client`` isn't importable."""
    try:
        import starmax_client  # noqa: F401
        return
    except ImportError:
        pass
    if os.path.isdir(os.path.join(STARMAX_CLIENT_DIR, "starmax_client")):
        if STARMAX_CLIENT_DIR not in sys.path:
            sys.path.insert(0, STARMAX_CLIENT_DIR)
