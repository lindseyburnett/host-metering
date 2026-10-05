import os
from contextlib import closing
from pathlib import Path
import subprocess
import sqlite3
import sys
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_packaged_migration_command(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run([sys.executable, str(ROOT / "tools/build.py"), "build",
                            "--distdir", directory], check=True)
            queue_path = Path(directory) / "queue"
            queue_path.mkdir()
            data = bytes.fromhex("0b0900000000000010401078")
            segment = queue_path / "00000000000000000001"
            segment.write_bytes(data)
            config = Path(directory) / "metering.conf"
            config.write_text("[host-metering]\nmetrics_wal_path={}\n".format(queue_path))
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("HOST_METERING_")}
            command = [sys.executable, str(Path(directory) / "host-metering"),
                       "--config", str(config), "migrate-wal"]
            for _ in range(2):
                result = subprocess.run(command, cwd=directory, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(segment.read_bytes(), data)
            with closing(sqlite3.connect(str(queue_path / "samples.sqlite3"))) as db:
                self.assertEqual(db.execute("SELECT value, timestamp FROM samples").fetchall(), [(4.0, 120)])

    def test_standalone_executable_and_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run([sys.executable, str(ROOT / "tools/build.py"), "build",
                            "--distdir", directory], check=True)
            executable = Path(directory) / "host-metering"
            result = subprocess.run([sys.executable, str(executable), "help"], cwd=directory,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"daemon", result.stdout)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("HOST_METERING_")}
            env["HOST_METERING_WRITE_RETRY_ATTEMPTS"] = "0"
            result = subprocess.run([sys.executable, str(executable), "--config", "/nonexistent", "once"],
                                    cwd=directory, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 2, result.stderr)
            with zipfile.ZipFile(str(executable)) as archive:
                self.assertIn("host_metering/daemon.py", archive.namelist())
                self.assertFalse(any("__pycache__" in name for name in archive.namelist()))

    def test_source_archive_can_build_without_git(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run([sys.executable, str(ROOT / "tools/build.py"), "tarball",
                            "--distdir", directory], check=True)
            tarball = next(Path(directory).glob("*.tar.gz"))
            with tarfile.open(str(tarball)) as archive:
                names = archive.getnames()
                self.assertFalse(any(name.endswith(".go") or "/.venv/" in name for name in names))
                self.assertFalse(any(name.endswith("host-metering-proxy-setup.sh") for name in names))
                # Only extract regular files from our locally generated archive.
                for member in archive.getmembers():
                    self.assertTrue(member.isfile())
                    self.assertNotIn("..", Path(member.name).parts)
                    target = Path(directory) / member.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source:
                        target.write_bytes(source.read())
            checkout = Path(directory) / names[0].split("/")[0]
            result = subprocess.run([sys.executable, "tools/build.py", "build"], cwd=str(checkout),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((checkout / "dist/host-metering").exists())
