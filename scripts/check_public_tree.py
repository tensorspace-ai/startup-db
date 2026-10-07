#!/usr/bin/env python3
"""Check indexed files without printing potentially sensitive matched values."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

PRIVATE_PARTS = {
    ".agent-runs",
    ".venv",
    ".git",
    ".codex",
    ".claude",
    ".copilot",
    ".ssh",
    ".aws",
    ".config",
    "private",
    "exports",
    "reports",
    "scratch",
    "tmp",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "python-dist",
}
PRIVATE_NAMES = {"agent.toml", "agent.local.toml", ".netrc", ".npmrc", ".pypirc"}
PRIVATE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db", ".log"}
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    "provider token": re.compile(
        r"\b(?:sk-(?:proj-|ant-)[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|"
        r"github_pat_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]{20,}|"
        r"AKIA[A-Z0-9]{16})\b"
    ),
    "credential assignment": re.compile(
        r"(?i)\b(?:api[_-]?key|password|client[_-]?secret|access[_-]?token)"
        r"\s*[=:]\s*[\"']([^\"'\s]{8,})[\"']"
    ),
    "credential URL": re.compile(r"https?://[^\s/@:]+:[^\s/@]+@"),
    "local home path": re.compile(r"(?:^|[\s\"'=])/(?:Users|home)/[\w.-]+/"),
}


def problems(path: str, blob: bytes, mode: str = "100644") -> list[str]:
    """Return categories and locations, never matched values."""
    file = PurePosixPath(path)
    lowered = path.lower()
    findings = []
    if (
        PRIVATE_PARTS.intersection(part.lower() for part in file.parts)
        or file.name.lower() in PRIVATE_NAMES
        or file.name.lower().startswith(".env")
        or file.suffix.lower() in PRIVATE_SUFFIXES
        or any(word in lowered for word in ("credentials", "secrets"))
        or (
            path.startswith("data/startups/")
            and any(part.startswith("_") for part in file.parts[2:])
        )
    ):
        findings.append("sensitive file path")
    if mode not in {"100644", "100755"}:
        findings.append("symlink or unsupported Git file mode")
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return [*findings, "non-text file requires explicit review"]
    if "\0" in text:
        findings.append("binary content")
    for number, line in enumerate(text.splitlines(), 1):
        for category, pattern in PATTERNS.items():
            if pattern.search(line):
                findings.append(f"line {number}: {category}")
    return findings


def main() -> int:
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    entries = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=root)
    failures = []
    count = 0
    # One batch process reads indexed blobs, not potentially different worktree files.
    with subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    ) as reader:
        assert reader.stdin is not None and reader.stdout is not None
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            metadata, encoded_path = entry.split(b"\t", 1)
            mode, oid, stage = metadata.decode().split()
            path = encoded_path.decode("utf-8")
            if stage != "0":
                failures.append((path, "unresolved index entry"))
                continue
            reader.stdin.write(oid.encode() + b"\n")
            reader.stdin.flush()
            header = reader.stdout.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                failures.append((path, "unsupported indexed object"))
                continue
            blob = reader.stdout.read(int(header[2]))
            reader.stdout.read(1)
            count += 1
            failures.extend((path, problem) for problem in problems(path, blob, mode))
        reader.stdin.close()
        reader.wait()
    for path, problem in failures:
        print(f"{path}: {problem}")
    if failures:
        print(f"Public-tree check failed: {len(failures)} finding(s).")
        return 1
    if not count:
        print("Public-tree check failed: index is empty.")
        return 1
    print(f"Public-tree check passed: {count} indexed text files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
