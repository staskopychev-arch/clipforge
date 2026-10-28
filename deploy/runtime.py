"""Prepare owned runtime paths, then drop root BEFORE serving or browsing."""
import os
import sys
import stat
from pathlib import Path

UID = GID = 10001


def prepare_data_directory(path):
    if path.is_symlink():
        raise RuntimeError("Runtime data directories must not be symlinks")
    path.mkdir(parents=True, exist_ok=True)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        # CAP_CHOWN allows bootstrap ownership, but is not CAP_FOWNER:
        # chmod must run while root still owns the directory.
        os.chown(path, 0, 0)
        path.chmod(0o700)
    os.chown(path, UID, GID)


def prepare(mode, *, administrative_transfer=False):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("The image entrypoint must initialize on Linux as root, then drop privileges")
    if Path(os.environ.get("MEDIA_ROOT", "")).resolve() != Path("/data"):
        raise RuntimeError("Runtime initialization is restricted to the /data volume")
    if mode in {"migrate", "tools"}:
        for name in ("", "sources", "clips", "browser", "health", "backups"):
            path = Path("/data") / name
            prepare_data_directory(path)
    # This home is a fresh tmpfs mount; image ownership is hidden by that mount.
    prepare_data_directory(Path("/home/clipforge"))
    # Compose binds secret files with host ownership. Keep them 0600 on the host;
    # copy only the granted secrets into this container's private tmpfs directory.
    destination = Path("/tmp/clipforge-secrets")
    destination.mkdir(mode=0o700, exist_ok=True)
    for key in ("APP_SECRET_FILE", "ADMIN_PASSWORD_HASH_FILE", "YOUTUBE_API_KEY_FILE"):
        source = Path(os.environ[key])
        value = source.read_bytes()
        if len(value) > 8192:
            raise RuntimeError("Oversized runtime secret")
        path = destination / key.lower()
        path.write_bytes(value)
        path.chmod(0o400)
        os.chown(path, UID, GID)
        os.environ[key] = str(path)
    os.chown(destination, UID, GID)
    os.environ["HOME"] = "/home/clipforge"
    os.environ["TMPDIR"] = "/tmp"
    os.umask(0o077)
    if not administrative_transfer:
        os.setgroups([])
        os.setgid(GID)
        os.setuid(UID)


def main():
    mode, *args = sys.argv[1:]
    if mode not in {"migrate", "web", "worker", "tools", "smoke"}:
        raise RuntimeError("Unknown runtime mode")
    # Only this fixed, non-browser data tool retains administrative file access
    # for private host bind mounts. All web, worker, smoke and other tools drop UID.
    administrative_transfer = mode == "tools" and args[:3] == ["python", "-m", "deploy.data_transfer"]
    prepare(mode, administrative_transfer=administrative_transfer)
    if mode == "migrate":
        command = [sys.executable, "-m", "deploy.migrate"]
    elif mode == "tools":
        if not args:
            raise RuntimeError("A tool command is required")
        command = args
    else:
        process = ([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000",
                    "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"] if mode == "web" else
                   [sys.executable, "-m", "deploy.worker_runner" if mode == "worker" else "deploy.browser_smoke"])
        command = ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1280x720x24 -nolisten tcp", *process]
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
