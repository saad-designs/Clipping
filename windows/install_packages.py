"""Installs Clipping's Python packages one at a time, retrying through a shaky connection.

Run by setup.bat. Each package gets up to 3 attempts with long timeouts, so a dropped
connection only repeats that one package instead of failing the whole install.
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["requirements-studio.txt", "requirements-watcher.txt"]
PIP = [sys.executable, "-m", "pip", "install", "--retries", "10", "--timeout", "90", "--disable-pip-version-check"]
# Backup download server (an official public mirror of PyPI), offered only if the normal one keeps failing.
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def requirements() -> list[str]:
    reqs = []
    for name in FILES:
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line and line not in reqs:
                reqs.append(line)
    return reqs


def install(req: str, extra: list[str]) -> bool:
    for attempt in range(1, 4):
        print(f"\n--- {req}  (attempt {attempt} of 3)", flush=True)
        if subprocess.run([*PIP, *extra, req]).returncode == 0:
            return True
        if attempt < 3:
            print("Connection problem; waiting 10 seconds and trying again...", flush=True)
            time.sleep(10)
    return False


def main() -> int:
    reqs = requirements()
    failed = [r for r in reqs if not install(r, [])]
    if failed:
        print("\nThese packages couldn't be downloaded from the normal server:", ", ".join(failed))
        answer = input("Try a backup download server for them? (Y/N): ").strip().lower()
        if answer.startswith("y"):
            failed = [r for r in failed if not install(r, ["-i", MIRROR])]
    if failed:
        print("\nStill missing:", ", ".join(failed))
        print("Your internet connection keeps dropping the downloads. Things that usually help:")
        print("  - run setup.bat again (finished packages are skipped, so it's quicker each time)")
        print("  - switch network (e.g. phone hotspot) or turn off any VPN while installing")
        return 1
    print("\nAll packages installed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
