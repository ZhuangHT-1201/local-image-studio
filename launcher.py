from __future__ import annotations

import subprocess
import sys
import time
import traceback
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


APP_DIR = Path(__file__).resolve().parent
SERVER = APP_DIR / "server.py"
URL = "http://127.0.0.1:8190"
ERROR_LOG = APP_DIR / "launcher-error.log"


def server_is_running(timeout: float = 4) -> bool:
    try:
        with urlopen(URL + "/api/health", timeout=timeout) as response:
            return response.status == 200
    except (URLError, TimeoutError, OSError):
        return False


def main() -> int:
    if server_is_running():
        webbrowser.open(URL)
        return 0

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [sys.executable, str(SERVER)],
        cwd=str(APP_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
        close_fds=True,
    )

    for _ in range(30):
        time.sleep(0.5)
        if server_is_running(timeout=2):
            webbrowser.open(URL)
            return 0

    ERROR_LOG.write_text(
        "简单生图界面启动超时。\n请检查 server.log 和 ComfyUI 状态。\n",
        encoding="utf-8",
    )
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        ERROR_LOG.write_text(traceback.format_exc(), encoding="utf-8")
        raise
