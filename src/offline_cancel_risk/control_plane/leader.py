"""Cross-process leader lock for control-plane ticks (file flock)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

_LOG = logging.getLogger(__name__)


class FileLeaderLock:
    """ponytail: flock on a lockfile — one control-plane leader per deploy volume."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = None

    def try_acquire(self) -> bool:
        if self._fh is not None:
            return True
        fh = open(self._path, "a+", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            return False
        except OSError:
            # Windows / unsupported — allow leadership (single-process demos)
            _LOG.warning(
                "fcntl flock unavailable; control-plane leader lock disabled"
            )
            self._fh = fh
            return True
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fh.close()
        finally:
            self._fh = None
