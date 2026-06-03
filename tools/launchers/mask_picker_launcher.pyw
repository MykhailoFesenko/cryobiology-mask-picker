from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "apps" / "mask_picker"
APP = APP_DIR / "app.py"
LOG_DIR = ROOT / "_logs"
LOG_FILE = LOG_DIR / "mask_picker_launcher.log"
HOST = "127.0.0.1"
PORT = 5000


def _server_is_up(host: str = HOST, port: int = PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _creationflags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def main() -> None:
    url = f"http://{HOST}:{PORT}"
    if _server_is_up():
        webbrowser.open(url)
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as log:
        log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] starting Mask Picker\n")
        log.flush()
        subprocess.Popen(
            [sys.executable, str(APP)],
            cwd=str(APP_DIR),
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=_creationflags(),
        )

    deadline = time.time() + 8.0
    while time.time() < deadline:
        if _server_is_up():
            webbrowser.open(url)
            return
        time.sleep(0.25)

    webbrowser.open(url)


if __name__ == "__main__":
    main()
