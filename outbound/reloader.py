"""Make sure each page runs the `outbound` code that is on disk, even right after a deploy.

Streamlit re-executes page scripts from disk on every run, but imported modules stay in
`sys.modules`. Streamlit normally drops changed modules for you, but that relies on its
file watcher, which can miss a deploy (e.g. a `git pull` on Streamlit Community Cloud).
The page then runs new code against old modules, which shows up as an AttributeError or
ImportError for anything that was just added.

`load_fresh` compares each module's file with a digest stored when it was last loaded,
and reloads it in place if the file changed. Call it at the top of every page, before
importing from `outbound`. Keep this module small and stable: it is the one piece that
can't refresh itself.
"""

from __future__ import annotations

import hashlib
import importlib
import sys
import threading
from pathlib import Path

_lock = threading.Lock()


def load_fresh(*module_names: str) -> None:
    """Import each module, reloading it if its source file changed since it was loaded.

    A module already in memory without a recorded digest (loaded before this tracker
    existed, e.g. by the previous deploy) is reloaded once, since it can't be verified.
    Pass dependencies before the modules that import them.
    """
    with _lock:
        for name in module_names:
            module = sys.modules.get(name)
            if module is None:
                module = importlib.import_module(name)  # first import reads the file on disk
            elif getattr(module, "__source_digest__", None) != _digest(module):
                module = importlib.reload(module)
            module.__source_digest__ = _digest(module)


def _digest(module) -> str | None:
    try:
        return hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    except (OSError, TypeError):
        return None
