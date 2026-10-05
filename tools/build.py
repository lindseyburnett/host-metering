"""Build standalone zipapps and source archives using only the standard library."""

import argparse
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import zipapp

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "host_metering/__init__.py"


def version():
    return re.search(r'__version__ = "([^"]+)"', VERSION_FILE.read_text()).group(1)


def build(destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        staging = Path(directory)
        shutil.copytree(str(ROOT / "host_metering"), str(staging / "host_metering"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (staging / "__main__.py").write_text(
            "from host_metering.cli import main\nraise SystemExit(main())\n")
        zipapp.create_archive(str(staging), str(destination / "host-metering"),
                              interpreter="/usr/bin/python3")


def source_files():
    roots = ["host_metering", "tests", "tools", "contrib", "mocks", ".github", ".devcontainer",
             ".vscode", ".copr", "Makefile", "README.md", "CONTRIBUTING.md", "CHANGELOG.md",
             "LICENSE", "setup.py", "pyproject.toml", "MANIFEST.in", ".gitignore", ".releaserc",
             ".packit.yaml", "commitlint.config.js"]
    ignored_dirs = {"__pycache__", "consumer", "cpumetrics", "tmp"}
    ignored_files = {"host-metering.spec", "host-metering.pp", "recording-rules.yml",
                     "docker-compose.local.yml"}
    for relative in roots:
        path = ROOT / relative
        if path.is_file():
            yield path
            continue
        for directory, dirs, files in os.walk(str(path)):
            dirs[:] = sorted(d for d in dirs if d not in ignored_dirs)
            for name in sorted(files):
                if (name in ignored_files or name.endswith(".pyc")
                        or name.startswith("subscription-manager-")):
                    continue
                yield Path(directory) / name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("build", "tarball", "version", "version-update", "spec"))
    parser.add_argument("--distdir", default=str(ROOT / "dist"))
    parser.add_argument("--version")
    parser.add_argument("--release", default="1")
    args = parser.parse_args()
    destination = Path(args.distdir)
    if args.command == "version":
        print(version())
    elif args.command == "version-update":
        if not args.version or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", args.version):
            parser.error("--version must be a semantic version")
        VERSION_FILE.write_text(re.sub(r'__version__ = "[^"]+"',
                                      '__version__ = "{}"'.format(args.version), VERSION_FILE.read_text()))
    elif args.command == "build":
        build(destination)
    elif args.command == "tarball":
        destination.mkdir(parents=True, exist_ok=True)
        prefix = "host-metering-" + version()
        with tarfile.open(str(destination / (prefix + ".tar.gz")), "w:gz") as archive:
            for path in source_files():
                archive.add(str(path), arcname=str(Path(prefix) / path.relative_to(ROOT)), recursive=False)
    elif args.command == "spec":
        template = (ROOT / "contrib/rpm/host-metering.spec.in").read_text()
        (ROOT / "contrib/rpm/host-metering.spec").write_text(
            template.replace("#VERSION#", version()).replace("#AUTORELEASE#", args.release))


if __name__ == "__main__":
    main()
