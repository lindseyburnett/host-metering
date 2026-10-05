"""Detect certificate/key edits, deletion, recreation and atomic replacement."""

from pathlib import Path


class CertWatcher:
    def __init__(self, *paths):
        self.paths = [Path(path) for path in paths]
        self._previous = self._snapshot()

    def _snapshot(self):
        result = []
        for path in self.paths:
            try:
                stat = path.stat()
                result.append((stat.st_dev, stat.st_ino, stat.st_size,
                               stat.st_mtime_ns, stat.st_ctime_ns))
            except FileNotFoundError:
                result.append(None)
        return result

    def changed(self):
        current = self._snapshot()
        changed = current != self._previous
        self._previous = current
        return changed
