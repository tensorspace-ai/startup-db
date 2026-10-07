"""Standard-library-only launch bridge; all agent logic lives in the uv package."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def launch(command: str, provider: str | None = None) -> None:
    root = Path(__file__).resolve().parent.parent
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv is required; install it from https://docs.astral.sh/uv/")
    arguments = sys.argv[1:]
    # The old preflight check is now part of the shared agent research contract.
    for index in range(len(arguments) - 1, -1, -1):
        if arguments[index].startswith("--url-timeout="):
            del arguments[index]
        elif arguments[index] == "--url-timeout":
            del arguments[index : index + 2]
    flags = ["--provider", provider] if provider else []
    os.execv(
        uv,
        [
            uv,
            "run",
            "--locked",
            "--project",
            str(root),
            "startup-agent",
            command,
            "--root",
            str(root),
            *flags,
            *arguments,
        ],
    )
