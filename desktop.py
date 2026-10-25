"""PySide6 desktop shell for local-brain."""

import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QIcon
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow

PROJECT_DIR = Path.home() / "local-brain"
ICON_PATH = PROJECT_DIR / "local_brain" / "static" / "brain-icon.png"
SERVER_URL = "http://127.0.0.1:9876/"


def server_is_up() -> bool:
    """True if something is already answering on the brain's port."""
    try:
        with urllib.request.urlopen(SERVER_URL, timeout=1):
            return True
    except Exception:
        return False


class LocalBrainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("local-brain")
        self.resize(1400, 900)

        self.browser = QWebEngineView()
        self.setCentralWidget(self.browser)
        self.browser.setUrl(QUrl(SERVER_URL))


def main():
    # Only start a server when one is not already up. Spawning unconditionally
    # meant a second uvicorn fought the running one for port 9876 and died,
    # while the wait loop below was satisfied by the *existing* server -- so the
    # shell came up fine but left a dead child behind.
    proc = None
    if not server_is_up():
        proc = subprocess.Popen(
            [str(PROJECT_DIR / "run.sh")],
            cwd=str(PROJECT_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(60):
            if server_is_up():
                break
            if proc.poll() is not None:
                print("local-brain server exited immediately; see its log", file=sys.stderr)
                sys.exit(1)
            time.sleep(0.25)
        else:
            print("Could not start local-brain server", file=sys.stderr)
            proc.terminate()
            sys.exit(1)

    app = QApplication(sys.argv)
    window = LocalBrainWindow()
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
        window.setWindowIcon(QIcon(str(ICON_PATH)))
    window.show()
    try:
        sys.exit(app.exec())
    finally:
        # Only clean up a server this process actually started; killing the
        # shared one would take down any other window or tab using it.
        if proc is not None:
            proc.terminate()


if __name__ == "__main__":
    main()
