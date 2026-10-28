"""One worker, signal forwarding and liveness heartbeat (not a success claim)."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main():
    child = subprocess.Popen([sys.executable, "-m", "app.worker"], start_new_session=True)
    def forward(signum, _frame):
        if child.poll() is None:
            os.killpg(child.pid, signum)
    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    path = Path("/data/health/worker.json")
    while child.poll() is None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"pid":child.pid, "timestamp":time.time()}), encoding="utf-8")
        temporary.replace(path)
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    raise SystemExit(child.returncode)


if __name__ == "__main__":
    main()
