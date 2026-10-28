"""Quiesced SQLite/media export and safe restore into an EMPTY destination.

The private archive contains encrypted sessions and must be treated as a secret.
The app encryption key is never included. No existing destination is overwritten.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory

MAX_BYTES = 32 * 1024**3


@contextmanager
def database_connection(*args, **kwargs):
    """Commit or roll back, then close the handle even on a failed transfer."""
    connection = sqlite3.connect(*args, **kwargs)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def media_name(kind, record_id):
    if kind not in {"sources", "clips"} or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", record_id):
        raise ValueError("Invalid media identity")
    return f"{kind}/{record_id}.mp4"


def music_names(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_plans'").fetchone():
        return set()
    result = set()
    for raw, in db.execute('SELECT config FROM automation_plans'):
        config = json.loads(raw)
        ids = {track['id'] for track in config.get('music_library', [])}
        selected = config.get('editing', {}).get('music_id')
        if selected and selected not in ids:
            raise ValueError('Selected music is missing from the library')
        for identity in ids:
            if not re.fullmatch(r'[a-f0-9]{32}', identity):
                raise ValueError('Invalid music identity')
            result.add('music/' + identity + '.m4a')
    return result


def export_data(database: Path, media_root: Path, output: Path):
    if output.exists():
        raise FileExistsError("Backup already exists; choose a new filename")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".clipforge-export-", dir=media_root) as temporary:
        root = Path(temporary)
        copied = root / "clipforge.db"
        with database_connection(database.resolve().as_uri() + "?mode=ro", uri=True) as source:
            active = source.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','processing')").fetchone()[0]
            if active:
                raise RuntimeError("Resolve pending jobs and stop producers before exporting; no tasks are cancelled automatically")
            with database_connection(copied) as target:
                source.backup(target)
        files = {"clipforge.db":copied}
        with database_connection(copied) as target:
            for name in music_names(target):
                path = media_root / name
                if path.is_symlink() or not path.is_file() or path.resolve().parent != (media_root/'music').resolve():
                    raise ValueError('Referenced music is missing or outside the music directory')
                files[name] = path
            for table, kind in (("source_videos", "sources"), ("clips", "clips")):
                for record_id, stored in target.execute(f"SELECT id, local_path FROM {table} WHERE local_path IS NOT NULL").fetchall():
                    name = media_name(kind, record_id)
                    expected = (media_root / name).resolve()
                    stored_path = Path(stored)
                    # Direct uploads and downloaded previews need not have the
                    # database UUID in their original filename. Preserve their
                    # bytes under the canonical archive name, without moving them.
                    if stored_path.is_absolute() and stored_path.is_file():
                        if stored_path.is_symlink():
                            raise ValueError("Media symlinks are not accepted")
                        actual = stored_path.resolve()
                        allowed_roots = ((media_root / "sources").resolve(), (media_root / "clips").resolve())
                        if not any(actual.is_relative_to(root) for root in allowed_roots):
                            raise ValueError("Media is outside the expected directory")
                    else:
                        # Compatibility with a verified Windows snapshot staged
                        # in canonical files before transfer to Linux.
                        if PureWindowsPath(stored).name != expected.name or not expected.is_file() or expected.is_symlink():
                            raise RuntimeError("Referenced media is missing or incompatible; repair the source path before export")
                        actual = expected
                    files[name] = actual
                    target.execute(f"UPDATE {table} SET local_path=? WHERE id=?", (name, record_id))
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or target.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("Database integrity check failed")
        manifest = {"format":1, "files":{name:{"sha256":digest(path), "size":path.stat().st_size} for name,path in files.items()}}
        created_output = False
        try:
            with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                created_output = True
                archive.writestr("manifest.json", json.dumps(manifest, indent=2))
                for name, path in files.items():
                    archive.write(path, name)
            output.chmod(0o600)
        except Exception:
            # Only the newly created partial output is removed, never user data.
            if created_output:
                output.unlink(missing_ok=True)
            raise
    return len(files)


def restore_data(archive_path: Path, media_root: Path):
    media_root = media_root.resolve()
    if any((media_root / item).exists() and
           ((media_root / item).is_file() or any((media_root / item).iterdir()))
           for item in ("clipforge.db", "sources", "clips", "music")):
        raise FileExistsError("Restore requires an empty destination. Keep the old volume for rollback.")
    media_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".clipforge-restore-", dir=media_root) as temporary:
        staging = Path(temporary)
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if len({item.filename for item in entries}) != len(entries) or sum(i.file_size for i in entries) > MAX_BYTES:
                raise ValueError("Duplicate entries or oversized backup")
            manifest_entry = archive.getinfo("manifest.json")
            if manifest_entry.file_size > 1024**2:
                raise ValueError("Oversized manifest")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != 1 or set(i.filename for i in entries) != {"manifest.json", *manifest["files"]}:
                raise ValueError("Unexpected backup contents")
            for name, expected in manifest["files"].items():
                path = PurePosixPath(name)
                is_music = len(path.parts)==2 and path.parts[0]=='music' and re.fullmatch(r'[a-f0-9]{32}\.m4a',path.name)
                if (name != "clipforge.db" and not is_music and
                    (len(path.parts) != 2 or path.parts[0] not in {"sources", "clips"} or
                     not re.fullmatch(r"[A-Za-z0-9_-]{1,64}\.mp4", path.name))):
                    raise ValueError("Unsafe backup filename")
                if path.is_absolute() or ".." in path.parts or "\\" in name:
                    raise ValueError("Unsafe backup path")
                entry = archive.getinfo(name)
                if entry.file_size != expected["size"] or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("Invalid backup entry")
                destination = staging / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target)
                if digest(destination) != expected["sha256"]:
                    raise ValueError("Backup checksum mismatch")
        with database_connection(staging / "clipforge.db") as db:
            if any(not (staging/name).is_file() for name in music_names(db)):
                raise ValueError('Backup references missing music')
            for table, kind in (("source_videos", "sources"), ("clips", "clips")):
                for record_id, stored in db.execute(f"SELECT id, local_path FROM {table} WHERE local_path IS NOT NULL").fetchall():
                    name = media_name(kind, record_id)
                    if stored != name or not (staging / name).is_file():
                        raise ValueError("Database references missing or unexpected media")
                    db.execute(f"UPDATE {table} SET local_path=? WHERE id=?", (str(media_root / name), record_id))
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Backup database integrity failed")
        media_root.mkdir(parents=True, exist_ok=True)
        for name in manifest["files"]:
            if name == "clipforge.db":
                continue
            path = media_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(staging / name, path)
            path.chmod(0o600)
            if os.environ.get("CLIPFORGE_CONTAINER") == "1" and hasattr(os, "geteuid") and os.geteuid() == 0:
                os.chown(path, 10001, 10001)
        # Install DB last; never overwrite an existing one.
        with (media_root / "clipforge.db").open("xb") as target, (staging / "clipforge.db").open("rb") as source:
            shutil.copyfileobj(source, target)
        (media_root / "clipforge.db").chmod(0o600)
        if os.environ.get("CLIPFORGE_CONTAINER") == "1" and hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(media_root / "clipforge.db", 10001, 10001)
    return len(manifest["files"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    export = sub.add_parser("export")
    export.add_argument("--database", type=Path, required=True)
    export.add_argument("--media-root", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--media-root", type=Path, required=True)
    args = parser.parse_args()
    count = (export_data(args.database, args.media_root, args.output) if args.mode == "export" else
             restore_data(args.archive, args.media_root))
    print(f"Private transfer verified: {count} files. Encryption key is not included.")


if __name__ == "__main__":
    main()
