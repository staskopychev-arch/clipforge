"""Interactive preparation. Never echoes or silently replaces credentials."""
import argparse
import base64
import getpass
import hashlib
import os
import secrets
import shutil
from pathlib import Path


def password_hash(password):
    if len(password) < 12:
        raise ValueError("Use an admin password of at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def initialize(folder: Path, password: str, app_secret: str, api_key: str):
    if len(app_secret) < 32:
        raise ValueError("Existing encryption key is too short; do not rotate it without re-encryption")
    encoded = password_hash(password)
    if folder.exists():
        raise FileExistsError("Secrets directory already exists; refusing to overwrite it")
    folder.mkdir(mode=0o700, parents=True)
    for name, value in {"app_secret":app_secret, "admin_password_hash":encoded, "youtube_api_key":api_key}.items():
        path = folder / name
        with path.open("x", encoding="utf-8") as stream:
            stream.write(value + "\n")
        path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--existing-env", type=Path, help="Local original .env: reuse encryption key and search API key, NOT its admin password")
    parser.add_argument("--directory", type=Path, default=Path("secrets"))
    args = parser.parse_args()
    existing = {}
    if args.existing_env:
        from dotenv import dotenv_values
        existing = dotenv_values(args.existing_env)
        if not existing.get("APP_SECRET"):
            raise ValueError("No APP_SECRET found in the original environment")
    password = getpass.getpass("New server admin password (12+ characters): ")
    if password != getpass.getpass("Confirm password: "):
        raise ValueError("Passwords do not match")
    api_key = existing.get("YOUTUBE_API_KEY") or getpass.getpass("YouTube search API key (blank to configure later): ")
    initialize(args.directory, password, existing.get("APP_SECRET") or secrets.token_urlsafe(48), api_key)
    if not Path(".env").exists():
        shutil.copyfile(".env.example", ".env")
        Path(".env").chmod(0o600)
    print("Private secrets prepared. Never put secrets/ or data backups in the public release archive.")


if __name__ == "__main__":
    main()
