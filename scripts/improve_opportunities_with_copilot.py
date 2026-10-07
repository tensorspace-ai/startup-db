#!/usr/bin/env python3
"""Compatibility launcher for `uv run startup-agent improve --provider copilot`."""

from _startup_agent import launch

if __name__ == "__main__":
    launch("improve", "copilot")
