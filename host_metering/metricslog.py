"""Durable sample queue, with transactional import of the former Go WAL."""

import fcntl
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

from .wire import Sample, decode_sample, read_varint


def legacy_samples(directory):
    """Read tidwall/wal v1.1.7's default binary segments without altering them.

    Each record is a varint length followed by a protobuf Sample. Empty records
    are checkpoints. START/END select committed truncations after a crash.
    Format: https://github.com/tidwall/wal/blob/v1.1.7/wal.go
    """
    segments = sorted(p for p in directory.iterdir() if p.is_file()
                      and re.fullmatch(r"[0-9]{20}(?:\.START|\.END)?", p.name)
                      and int(p.name[:20]) > 0)
    starts = [i for i, p in enumerate(segments) if p.suffix == ".START"]
    ends = [i for i, p in enumerate(segments) if p.suffix == ".END"]
    if len(starts) > 1 or len(ends) > 1 or (starts and ends):
        raise ValueError("Inconsistent legacy WAL truncation markers")
    if starts:
        segments = segments[starts[0]:]
    elif ends:
        segments = segments[:ends[0] + 1]
        if len(segments) > 1 and segments[-2].name[:20] == segments[-1].name[:20]:
            del segments[-2]
    expected = None
    for segment in segments:
        index = int(segment.name[:20])
        if expected is not None and index != expected:
            raise ValueError("Non-contiguous legacy WAL at " + str(segment))
        data = segment.read_bytes()
        offset = 0
        while offset < len(data):
            size, offset = read_varint(data, offset)
            end = offset + size
            if end > len(data):
                raise ValueError("Truncated legacy WAL at " + str(segment))
            if size:
                yield decode_sample(data[offset:end])
            offset = end
            index += 1
        expected = index


class MetricsLog:
    def __init__(self, path):
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
            with self._db:
                imported = self._db.execute(
                    "SELECT value FROM metadata WHERE key='legacy_imported'"
                ).fetchone()
                if imported is None:
                    self._db.executemany(
                        "INSERT INTO samples(value, timestamp) VALUES (?, ?)",
                        legacy_samples(self.path),
                    )
                    self._db.execute("INSERT INTO metadata VALUES ('legacy_imported', '1')")
        except BaseException:
            self.close()
            raise

    def write_sample(self, cpu_count, timestamp=None):
        if timestamp is None:
            timestamp = int(time.time() * 1000)
        with self._mutex, self._db:
            self._db.execute("INSERT INTO samples(value, timestamp) VALUES (?, ?)",
                             (float(cpu_count), timestamp))

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
