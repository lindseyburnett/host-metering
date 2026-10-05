"""Durable SQLite sample queue."""

import fcntl
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

from .wire import Sample


def legacy_segments(directory):
    """Recognize old queue files without loading their decoder at startup."""
    return sorted(p for p in directory.iterdir() if p.is_file()
                  and re.fullmatch(r"[0-9]{20}(?:\.START|\.END)?", p.name)
                  and int(p.name[:20]) > 0)


class MetricsLog:
    def __init__(self, path, allow_legacy=False):
        if not path:
            raise ValueError("metrics log path cannot be empty")
        self.path = Path(path)
        self.path.mkdir(mode=0o750, parents=True, exist_ok=True)
        self._mutex = threading.RLock()
        self._db = None
        self._lock_file = os.open(str(self.path / ".lock"), os.O_CREAT | os.O_RDWR, 0o640)
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            db_path = self.path / "samples.sqlite3"
            fd = os.open(str(db_path), os.O_CREAT | os.O_RDWR, 0o640)
            os.close(fd)
            self._db = sqlite3.connect(str(db_path), check_same_thread=False)
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("CREATE TABLE IF NOT EXISTS samples ("
                             "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                             "value REAL NOT NULL, timestamp INTEGER NOT NULL)")
            self._db.execute("CREATE INDEX IF NOT EXISTS sample_time ON samples(timestamp)")
            self._db.execute("CREATE TABLE IF NOT EXISTS metadata ("
                             "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self._db.commit()
            if not allow_legacy and legacy_segments(self.path):
                if not self._db.execute(
                    "SELECT 1 FROM metadata WHERE key='legacy_imported'"
                ).fetchone():
                    raise ValueError(
                        "Unmigrated WAL files found in {}. Stop the old service, then run "
                        "host-metering migrate-wal with the same --config or "
                        "HOST_METERING_METRICS_WAL_PATH before starting the daemon.".format(self.path)
                    )
        except BaseException:
            self.close()
            raise

    def write_sample(self, cpu_count, timestamp=None):
        if timestamp is None:
            timestamp = int(time.time() * 1000)
        with self._mutex, self._db:
            self._db.execute("INSERT INTO samples(value, timestamp) VALUES (?, ?)",
                             (float(cpu_count), timestamp))

    def import_samples_once(self, marker, samples):
        """Commit an iterable and its migration marker atomically.

        Return the inserted count, or None if this import already completed.
        The queue lock excludes daemon activity while migration is in progress.
        """
        with self._mutex, self._db:
            if self._db.execute("SELECT 1 FROM metadata WHERE key=?", (marker,)).fetchone():
                return None
            count = 0
            for sample in samples:
                self._db.execute("INSERT INTO samples(value, timestamp) VALUES (?, ?)", sample)
                count += 1
            self._db.execute("INSERT INTO metadata VALUES (?, '1')", (marker,))
            return count

    def get_samples(self):
        with self._mutex:
            rows = self._db.execute(
                "SELECT id, value, timestamp FROM samples ORDER BY timestamp, id"
            ).fetchall()
        return ([Sample(value, timestamp) for _, value, timestamp in rows],
                max((row[0] for row in rows), default=0))

    def remove_samples(self, checkpoint):
        # Autoincrement prevents an old snapshot from deleting newer samples.
        with self._mutex, self._db:
            self._db.execute("DELETE FROM samples WHERE id <= ?", (checkpoint,))

    def remove_expired(self, max_age, now=None):
        if max_age <= 0:
            return
        threshold = int((time.time() if now is None else now) * 1000) - max_age * 1000
        with self._mutex, self._db:
            self._db.execute("DELETE FROM samples WHERE timestamp < ?", (threshold,))

    def close(self):
        with self._mutex:
            if self._db is not None:
                self._db.close()
                self._db = None
            if self._lock_file is not None:
                os.close(self._lock_file)
                self._lock_file = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
