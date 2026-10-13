"""Pack the project into ai_civ_docker.tar.gz for deployment (stdlib only).

Usage: python -m scripts.deploy_pack [--output PATH]
"""
from __future__ import annotations

import argparse
import fnmatch
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_NAME = "ai_civ_docker.tar.gz"

# Must exist, otherwise the archive is useless -> fail fast.
REQUIRED = ["src", "migrations", "pyproject.toml", "alembic.ini", "docker-compose.yml", "Dockerfile", ".env"]
# Packed when present (a missing one is only a warning).
OPTIONAL = [".env.example", "CLAUDE.md", "README.md", ".dockerignore", "web/dist"]

EXCLUDE_DIRS = {".venv", "venv", "__pycache__", ".git", "tests", ".pytest_cache", "data", "logs",
                "node_modules", ".run", ".mypy_cache", ".ruff_cache", "*.egg-info"}
EXCLUDE_FILES = ["*.db", "*.pyc", "*.pyo", "celerybeat-schedule*", "*.sqlite", "*.sqlite3", "*.log"]

EXEC_SUFFIXES = {".sh"}


def _excluded(rel: Path) -> bool:
    if any(fnmatch.fnmatch(part, pat) for part in rel.parts[:-1] for pat in EXCLUDE_DIRS):
        return True
    name = rel.name
    if (ROOT / rel).is_dir() and any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_DIRS):
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_FILES)


def _is_executable(path: Path) -> bool:
    if path.suffix in EXEC_SUFFIXES:
        return True
    try:
        with path.open("rb") as f:
            return f.read(2) == b"#!"
    except OSError:
        return False


def _filter(info: tarfile.TarInfo, source: Path) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = int(source.stat().st_mtime)
    if info.isdir():
        info.mode = 0o755
    else:
        info.mode = 0o755 if _is_executable(source) else 0o644
    return info


def collect() -> list[Path]:
    """Return relative paths of every file to pack."""
    missing = [n for n in REQUIRED if not (ROOT / n).exists()]
    if missing:
        raise FileNotFoundError("Required files are missing: " + ", ".join(missing))
    files: list[Path] = []
    for name in REQUIRED + OPTIONAL:
        path = ROOT / name
        if not path.exists():
            print(f"  warning: optional {name} not found, skipped", file=sys.stderr)
            continue
        candidates = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
        for p in candidates:
            rel = p.relative_to(ROOT)
            if not _excluded(rel):
                files.append(rel)
    return files


def build_archive(output: Path | None = None) -> Path:
    """Build the archive and return its path."""
    output = Path(output) if output else ROOT / ARCHIVE_NAME
    files = collect()
    dirs: set[Path] = set()
    for rel in files:
        dirs.update(p for p in rel.parents if p != Path("."))
    with tarfile.open(output, "w:gz") as tar:
        for d in sorted(dirs):
            tar.add(ROOT / d, arcname=d.as_posix(), recursive=False, filter=lambda i, s=ROOT / d: _filter(i, s))
        for rel in files:
            src = ROOT / rel
            tar.add(src, arcname=rel.as_posix(), recursive=False, filter=lambda i, s=src: _filter(i, s))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None, help=f"archive path (default: ./{ARCHIVE_NAME})")
    args = parser.parse_args()
    try:
        path = build_archive(args.output)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{path} ({path.stat().st_size / 1024 / 1024:.2f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
