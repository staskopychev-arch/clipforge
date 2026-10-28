"""Host-side Docker Compose commands. No shell interpolation or data deletion."""
import argparse
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--https", action="store_true")
    parser.add_argument("--project", default="clipforge-server")
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("build", "start", "status", "logs", "smoke", "backup"):
        sub.add_parser(name)
    restore = sub.add_parser("restore")
    restore.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", args.project):
        raise ValueError("Invalid Compose project name")
    base = ["docker", "compose", "-p", args.project, "-f", "compose.yml"]
    if args.https:
        base += ["-f", "compose.https.yml"]
    def run(*command):
        subprocess.run([*base, *command], check=True)
    if args.mode == "build":
        run("build")
    elif args.mode == "start":
        # Re-run migration and apply changed secrets/environment on every start.
        run("up", "-d", "--force-recreate")
    elif args.mode == "status":
        run("ps", "-a")
    elif args.mode == "logs":
        run("logs", "--tail", "100", "web", "worker", "migrate")
    elif args.mode == "smoke":
        run("run", "--rm", "--no-deps", "worker", "smoke")
    elif args.mode == "backup":
        folder = Path("backups").resolve()
        folder.mkdir(mode=0o700, exist_ok=True)
        filename = datetime.now(timezone.utc).strftime("clipforge-%Y%m%d-%H%M%S.zip")
        run("stop", "web", "worker")
        try:
            run("run", "--rm", "--no-deps", "migrate", "tools",
                "python", "-m", "deploy.data_transfer", "export", "--database", "/data/clipforge.db",
                "--media-root", "/data", "--output", f"/data/backups/{filename}")
            run("cp", f"worker:/data/backups/{filename}", str(folder / filename))
            (folder / filename).chmod(0o600)
        finally:
            run("up", "-d", "web", "worker")
    else:
        archive = args.archive.resolve(strict=True)
        run("run", "--rm", "--no-deps", "-v", f"{archive}:/transfer.zip:ro", "migrate", "tools",
            "python", "-m", "deploy.data_transfer", "restore", "--archive", "/transfer.zip", "--media-root", "/data")


if __name__ == "__main__":
    main()
