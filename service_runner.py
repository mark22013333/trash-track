"""同時監督網站與 GPS 蒐集器，確保 Ctrl+C 一併安全停止。"""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
IS_WINDOWS = sys.platform == "win32"
CHILD_CREATION_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
children: list[subprocess.Popen] = []
stopping = False


def stop_children(*_args) -> None:
    global stopping
    if stopping:
        return
    stopping = True
    for child in children:
        if child.poll() is None:
            if IS_WINDOWS:
                try:
                    child.send_signal(signal.CTRL_BREAK_EVENT)
                    continue
                except (OSError, ValueError):
                    pass
            child.terminate()


def main() -> int:
    handled_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        handled_signals.append(signal.SIGBREAK)
    for signal_name in handled_signals:
        signal.signal(signal_name, stop_children)
    children.extend([
        subprocess.Popen(
            [sys.executable, str(BASE_DIR / "collector.py")],
            cwd=BASE_DIR,
            creationflags=CHILD_CREATION_FLAGS,
        ),
        subprocess.Popen(
            [sys.executable, str(BASE_DIR / "app.py")],
            cwd=BASE_DIR,
            creationflags=CHILD_CREATION_FLAGS,
        ),
    ])
    try:
        return children[1].wait()
    finally:
        stop_children()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
