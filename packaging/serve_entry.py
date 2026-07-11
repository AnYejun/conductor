"""PyInstaller entry point for the headless engine (Tauri sidecar)."""
import argparse
import sys

from conductor.app import ensure_default_plan, run_headless


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--plan", default=None)
    p.add_argument("--parent-pid", type=int, default=None)
    a = p.parse_args()
    from pathlib import Path
    plan = Path(a.plan).expanduser() if a.plan else ensure_default_plan()
    return run_headless(plan, a.port, a.parent_pid)


if __name__ == "__main__":
    sys.exit(main())
