"""PySide6 desktop shell for local-brain."""

import sys
import subprocess
import time

from PySide6.QtCore import QUrl
from PySide6.QtGui import QIcon
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow

ICON_PATH = Path.home() / "local-brain" / "local_brain" / "static" / "brain-icon.png"


class LocalBrainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("local-brain")
        self.resize(1400, 900)

        self.browser = QWebEngineView()
        self.setCentralWidget(self.browser)
        self.browser.setUrl(QUrl("http://127.0.0.1:9876/"))


def main():
    # Start backend server
    proc = subprocess.Popen(
        ["bash", "-c", "cd ~/local-brain && ./run.sh"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for server to come up
    for _ in range(30):
        try:
            import urllib.request
            urllib.request.urlopen("http://127.0.0.1:9876/", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
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
        proc.terminate()


if __name__ == "__main__":
    main()
