import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

from ..config import settings
from ..models import Account, RecordStatus
from ..security import decrypt, encrypt
from .media import choose_chunk_plan, probe_duration


API_BASE = "https://open.tiktokapis.com"
AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"


class TikTokError(RuntimeError):
    pass


TERMINAL_PUBLISH_STATUSES = {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX", "FAILED"}


def _raise_api_error(payload: dict, fallback: str) -> None:
    error = payload.get("error", {})
    if error.get("code") in (None, "ok"):
        return
    detail = error.get("message") or error.get("code") or fallback
    log_id = error.get("log_id")
    if log_id:
        detail = f"{detail} (TikTok log_id: {log_id})"
    raise TikTokError(detail)


def _truncate_utf16(value: str, max_units: int) -> str:
    """TikTok measures title length in UTF-16 code units, not Python characters."""
    encoded = value.encode("utf-16-le")
    if len(encoded) <= max_units * 2:
        return value
    truncated = encoded[: max_units * 2]
    while truncated:
        try:
            return truncated.decode("utf-16-le")
        except UnicodeDecodeError:
            truncated = truncated[:-2]
    return ""


def authorization_url(state: str) -> str:
    query = urlencode(
        {
            "client_key": settings.tiktok_client_key,
            "response_type": "code",
            "scope": settings.tiktok_scopes,
            "redirect_uri": settings.tiktok_redirect_uri,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def exchange_code(code: str) -> dict:
    response = httpx.post(
        f"{API_BASE}/v2/oauth/token/",
        data={
            "client_key": settings.tiktok_client_key,
            "client_secret": settings.tiktok_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": settings.tiktok_redirect_uri,
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("error") or not data.get("access_token"):
        raise TikTokError(data.get("error_description") or data.get("error") or "Token exchange failed")
    return data


def apply_token_data(account: Account, data: dict) -> None:
    now = datetime.now(timezone.utc)
    account.external_id = data["open_id"]
    account.access_token = encrypt(data["access_token"])
    account.refresh_token = encrypt(data.get("refresh_token", ""))
    account.access_expires_at = now + timedelta(seconds=int(data.get("expires_in", 0)))
    account.refresh_expires_at = now + timedelta(seconds=int(data.get("refresh_expires_in", 0)))
    account.status = RecordStatus.ready


def refresh_account(account: Account) -> str:
    now = datetime.now(timezone.utc)
    access_expiry = account.access_expires_at
    if access_expiry and access_expiry.tzinfo is None:
        access_expiry = access_expiry.replace(tzinfo=timezone.utc)
    if access_expiry and access_expiry > now + timedelta(minutes=5):
        return decrypt(account.access_token)
    response = httpx.post(
        f"{API_BASE}/v2/oauth/token/",
        data={
            "client_key": settings.tiktok_client_key,
            "client_secret": settings.tiktok_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": decrypt(account.refresh_token),
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("error") or not data.get("access_token"):
        account.status = RecordStatus.reauth_required
        raise TikTokError(data.get("error_description") or data.get("error") or "Token refresh failed")
    apply_token_data(account, data)
    return decrypt(account.access_token)


def creator_info(access_token: str) -> dict:
    response = httpx.post(
        f"{API_BASE}/v2/post/publish/creator_info/query/",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    _raise_api_error(payload, "Creator info request failed")
    return payload.get("data", {})


def _upload_chunks(upload_url: str, video_path: Path, chunk_size: int, chunk_count: int) -> None:
    total = video_path.stat().st_size
    with video_path.open("rb") as file_handle, httpx.Client(timeout=180) as client:
        start = 0
        for index in range(chunk_count):
            expected = chunk_size if index < chunk_count - 1 else total - start
            chunk = file_handle.read(expected)
            if not chunk:
                raise TikTokError("Unexpected end of video while uploading")
            end = start + len(chunk) - 1
            response = client.put(
                upload_url,
                content=chunk,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{total}",
                },
            )
            expected_status = 201 if index == chunk_count - 1 else 206
            if response.status_code != expected_status:
                raise TikTokError(f"TikTok upload failed: HTTP {response.status_code} {response.text[:300]}")
            start = end + 1


def fetch_publish_status(access_token: str, publish_id: str) -> dict:
    response = httpx.post(
        f"{API_BASE}/v2/post/publish/status/fetch/",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"},
        json={"publish_id": publish_id},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    _raise_api_error(payload, "TikTok status request failed")
    return payload.get("data", {})


def wait_for_publish(access_token: str, publish_id: str) -> dict:
    deadline = time.monotonic() + settings.tiktok_status_timeout_seconds
    latest: dict = {"status": "PROCESSING_UPLOAD"}
    while time.monotonic() < deadline:
        latest = fetch_publish_status(access_token, publish_id)
        status = latest.get("status")
        if status in TERMINAL_PUBLISH_STATUSES:
            if status == "FAILED":
                raise TikTokError(f"TikTok processing failed: {latest.get('fail_reason') or 'unknown reason'}")
            return latest
        time.sleep(settings.tiktok_status_poll_seconds)
    # Processing and moderation may legitimately continue after our polling window.
    return {**latest, "polling_timed_out": True}


def publish_video(
    account: Account,
    video_path: Path,
    caption: str,
    privacy_level: str,
    mode: str,
) -> dict:
    access_token = refresh_account(account)
    info = creator_info(access_token)
    allowed_privacy = info.get("privacy_level_options", [])
    if mode == "direct" and privacy_level not in allowed_privacy:
        raise TikTokError(f"Privacy level is not allowed for this creator: {allowed_privacy}")

    max_duration = info.get("max_video_post_duration_sec")
    duration = probe_duration(video_path)
    if max_duration and duration > float(max_duration) + 0.25:
        raise TikTokError(
            f"Video is {duration:.1f}s long, but this creator allows at most {float(max_duration):.1f}s"
        )

    size = video_path.stat().st_size
    chunk_size, chunk_count = choose_chunk_plan(size)
    source_info = {
        "source": "FILE_UPLOAD",
        "video_size": size,
        "chunk_size": chunk_size,
        "total_chunk_count": chunk_count,
    }
    if mode == "direct":
        endpoint = "/v2/post/publish/video/init/"
        body = {
            "post_info": {
                "title": _truncate_utf16(caption, 2200),
                "privacy_level": privacy_level,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
                "video_cover_timestamp_ms": 1000,
                "brand_content_toggle": False,
                "brand_organic_toggle": False,
                "is_aigc": False,
            },
            "source_info": source_info,
        }
    else:
        endpoint = "/v2/post/publish/inbox/video/init/"
        body = {"source_info": source_info}

    response = httpx.post(
        f"{API_BASE}{endpoint}",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    _raise_api_error(payload, "TikTok publish initialization failed")
    data = payload["data"]
    _upload_chunks(data["upload_url"], video_path, chunk_size, chunk_count)
    status = wait_for_publish(access_token, data["publish_id"])
    return {"publish_id": data["publish_id"], "mode": mode, **status}

