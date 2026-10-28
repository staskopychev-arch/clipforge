import json
import os
import sys
import time
from pathlib import Path
from urllib.request import urlopen


def main():
    if sys.platform == "linux" and os.geteuid() == 0:
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    if sys.argv[1] == "web":
        with urlopen("http://127.0.0.1:8000/health", timeout=4) as response:
            assert json.load(response)["status"] == "ok"
    else:
        heartbeat = json.loads(Path("/data/health/worker.json").read_text())
        assert 0 <= time.time() - heartbeat["timestamp"] < 45
        os.kill(int(heartbeat["pid"]), 0)


if __name__ == "__main__":
    main()
