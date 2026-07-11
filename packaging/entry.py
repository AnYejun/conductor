"""PyInstaller entry point for the Conductor desktop app."""
import sys

from conductor.app import desktop_main

if __name__ == "__main__":
    sys.exit(desktop_main())
