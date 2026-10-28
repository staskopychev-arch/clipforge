"""Read-only server acceptance: real network/browser/media, never uploads videos."""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from sqlalchemy import select, text
from playwright.sync_api import sync_playwright

from app.config import settings
from app.db import SessionLocal
from app.models import Account, SourceVideo, Clip, RecordStatus
from app.security import decrypt
from app.services.browser_network import isolated_youtube_browser
from app.services.youtube import search_creative_commons
from app.services.youtube_upload import check_session, YouTubeSessionError, connection_error
from app.services.y2save import Y2SaveDownloader
from app.services.media import probe_duration, render_vertical_clip


def database_check():
    with SessionLocal() as db:
        assert db.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert not db.execute(text("PRAGMA foreign_key_check")).all()
        accounts = db.scalars(select(Account).where(Account.platform == "youtube").order_by(Account.created_at.desc())).all()
        for account in accounts:
            state = json.loads(decrypt(account.access_token))
            assert isinstance(state.get("cookies"), list), "Invalid encrypted account state"
        from pathlib import Path
        for model in (SourceVideo, Clip):
            for item in db.scalars(select(model).where(model.local_path.is_not(None))).all():
                path = Path(item.local_path)
                assert path.is_relative_to(Path("/data")) and path.is_file(), "Media path not transferred"
        print(json.dumps({"database":"ok", "encrypted_accounts":len(accounts), "media_paths":"ok"}), flush=True)
        return accounts


def network_check(url="https://studio.youtube.com"):
    with sync_playwright() as driver:
        with isolated_youtube_browser(driver, headless=False) as browser:
            context = browser.new_context()
            page = context.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=90000)
            assert response and response.status < 500
            expected = ({"studio.youtube.com", "www.youtube.com", "accounts.google.com", "consent.youtube.com"} if "studio.youtube.com" in url else {"y2save.com", "www.y2save.com"})
            assert urlsplit(page.url).hostname in expected
            print(json.dumps({"browser_https":"ok", "target_host":urlsplit(url).hostname, "landing_host":urlsplit(page.url).hostname}), flush=True)
            context.close()


def session_check(accounts):
    if not accounts:
        raise RuntimeError("No YouTube accounts were transferred")
    usable = False
    candidates = sorted(accounts, key=lambda account: account.status != RecordStatus.ready)
    for number, account in enumerate(candidates, start=1):
        try:
            check_session(account.access_token)
        except YouTubeSessionError:
            print(json.dumps({"account_number":number, "youtube_session":"manual_login_required"}), flush=True)
        except Exception as error:
            print(json.dumps({"account_number":number, "youtube_session":"connection_error", "detail":connection_error(error)}, ensure_ascii=False), flush=True)
        else:
            usable = True
            print(json.dumps({"account_number":number, "youtube_session":"authorized"}), flush=True)
    return usable


def search_check():
    results = search_creative_commons("nature landscape", 1)
    print(json.dumps({"youtube_search":"ok", "licensed_results":len(results)}), flush=True)


def downloader_check():
    downloader = Y2SaveDownloader()
    try:
        downloader.start()
        page = downloader._ensure_site()
        downloader._raise_if_challenged(page)
        assert downloader._input(page).is_visible()
        assert downloader._submit(page).is_visible()
        print(json.dumps({"downloader_form":"ok", "file_download":"not_performed"}), flush=True)
    finally:
        downloader.close()


def download_check():
    # Use a short, license-verified CC result only for private validation.
    results = search_creative_commons("nature landscape", 1, "short")
    if not results:
        raise RuntimeError("No short CC test source available")
    downloader = Y2SaveDownloader()
    try:
        with TemporaryDirectory(prefix=".acceptance-download-", dir=settings.media_root) as directory:
            source, clip = Path(directory) / "source.mp4", Path(directory) / "clip.mp4"
            downloader.download(results[0]["url"], source)
            duration = probe_duration(source)
            assert duration >= 5
            render_vertical_clip(source, clip, 0, 5)
            assert 4.9 <= probe_duration(clip) <= 5.2
            print(json.dumps({"file_download":"ok", "source_bytes":source.stat().st_size,
                              "source_url":results[0]["url"], "render":"ok", "publication":"not_performed"}), flush=True)
    finally:
        downloader.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--session", action="store_true")
    parser.add_argument("--search", action="store_true")
    parser.add_argument("--downloader", action="store_true")
    parser.add_argument("--downloader-network", action="store_true")
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    accounts = database_check()
    if args.network:
        network_check()
    if args.downloader_network:
        network_check("https://y2save.com/en")
    if args.search:
        search_check()
    if args.downloader:
        downloader_check()
    if args.download:
        download_check()
    if args.session and not session_check(accounts):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
