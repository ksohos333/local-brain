"""Start the local-brain server as a detached daemon."""

import os
import subprocess
import sys
from pathlib import Path

LOG_FILE = Path.home() / ".local" / "share" / "local-brain" / "server.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def is_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-f", "uvicorn local_brain.main:app"],
        capture_output=True,
    )
    return result.returncode == 0


def start():
    if is_running():
        print("local-brain server is already running.")
        return

    venv_python = Path.home() / "local-brain" / ".venv" / "bin" / "python"
    if not venv_python.exists():
        print(f"Virtual env not found: {venv_python}")
        sys.exit(1)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.home() / "local-brain")

    with open(LOG_FILE, "a") as log:
        subprocess.Popen(
            [str(venv_python), "-m", "uvicorn", "local_brain.main:app", "--host", "127.0.0.1", "--port", "9876"],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            cwd=str(Path.home() / "local-brain"),
            env=env,
            start_new_session=True,
        )

    # Wait briefly for startup
    import time
    for _ in range(20):
        if is_running():
            print("local-brain server started.")
            return
        time.sleep(0.2)
    print("Server may not have started. Check log.")


if __name__ == "__main__":
    start()
