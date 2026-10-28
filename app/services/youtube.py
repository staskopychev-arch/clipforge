import html
import re
import unicodedata

import httpx

from ..config import settings


class YouTubeSearchError(RuntimeError):
    pass


class YouTubeNoResultsError(RuntimeError):
    def __init__(self, attempts):
        self.attempts = attempts
        queries = "; ".join(f'«{item["query"]}»' for item in attempts)
        super().__init__(f"Поиск не нашёл подходящих роликов. Проверены запросы: {queries}. Измените запрос или добавьте свой исходник")


SEARCH_RIGHTS_MODE = "author_permission"
SEARCH_RIGHTS_LABEL = "Есть разрешение автора"


class SearchResults(list):
    def __init__(self, items=(), next_page_token=None):
        super().__init__(items)
        self.next_page_token = next_page_token


def source_attribution(source, rights_basis: str = SEARCH_RIGHTS_MODE, *, title_limit: int | None = None) -> str:
    title = source.title[:title_limit] if title_limit else source.title
    terms = ("Creative Commons Attribution; excerpt and vertical edit." if rights_basis == "creative_commons"
             else "Used with creator permission; excerpt and vertical edit.")
    return f"Source: {title} — {source.source_url}\n{terms}"


def search_youtube(query: str, limit: int, duration_filter: str = "any", *, page_token=None) -> list[dict]:
    """Search normally; the uploader declares permission independently of YouTube's license label."""
    return _search(query, limit, duration_filter, SEARCH_RIGHTS_MODE, page_token=page_token)


def search_creative_commons(query: str, limit: int, duration_filter: str = "any") -> list[dict]:
    """Explicit legacy utility; automatic workflows use search_youtube instead."""
    return _search(query, limit, duration_filter, "creative_commons")


def _search(query: str, limit: int, duration_filter: str, rights_basis: str, *, page_token=None) -> list[dict]:
    query = query.strip()
    if not query:
        raise YouTubeSearchError("Укажите поисковый запрос")
    if not settings.youtube_api_key:
        raise YouTubeSearchError("YOUTUBE_API_KEY is not configured")
    limit = min(50, max(1, limit))
    params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": limit,
            "videoDuration": duration_filter if duration_filter in {"any", "long", "medium", "short"} else "any",
            "videoEmbeddable": "true",
            "safeSearch": "strict",
            "order": "relevance",
            "key": settings.youtube_api_key,
        }
    if rights_basis == "creative_commons":
        params["videoLicense"] = "creativeCommon"
    if page_token:
        params['pageToken'] = page_token
    response = httpx.get(
        "https://www.googleapis.com/youtube/v3/search",
        params=params,
        timeout=30,
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("error", {}).get("message")
        except ValueError:
            detail = response.text[:300]
        raise YouTubeSearchError(detail or f"YouTube search returned HTTP {response.status_code}")
    results: list[dict] = []
    for item in response.json().get("items", []):
        video_id = item.get("id", {}).get("videoId")
        snippet = item.get("snippet", {})
        if not video_id:
            continue
        results.append(
            {
                "video_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "title": html.unescape(snippet.get("title", "YouTube video")),
                "channel": html.unescape(snippet.get("channelTitle", "")),
                "description": html.unescape(snippet.get("description", "")),
                "rights_basis": rights_basis,
            }
        )
    if rights_basis == SEARCH_RIGHTS_MODE:
        return SearchResults(results[:limit], response.json().get('nextPageToken'))
    # The explicit CC utility still verifies the actual video license.
    if not results:
        return []
    verification = httpx.get("https://www.googleapis.com/youtube/v3/videos", params={
        "part": "status", "id": ",".join(item["video_id"] for item in results), "key": settings.youtube_api_key,
    }, timeout=30)
    if verification.status_code >= 400:
        raise YouTubeSearchError(f"Не удалось проверить лицензии: HTTP {verification.status_code}")
    licensed_ids = {item["id"] for item in verification.json().get("items", []) if item.get("status", {}).get("license") == "creativeCommon"}
    return [item for item in results if item["video_id"] in licensed_ids][:limit]



SEARCH_WINDOW = 20


def relevance_score(query: str, item: dict) -> float:
    """Compare metadata relevance; YouTube order breaks equal scores.

    This does not claim to inspect the actual video frames.
    """
    def tokens(text):
        text = unicodedata.normalize("NFKC", html.unescape(text)).casefold()
        return re.findall(r"[^\W_]+", text, flags=re.UNICODE)

    requested = tokens(query)
    wanted = set(requested)
    if not wanted:
        return 0.0
    title = tokens(item.get("title", ""))
    description = set(tokens(item.get("description", "")))
    coverage = len(wanted.intersection(title)) / len(wanted)
    supporting = len(wanted.intersection(description)) / len(wanted)
    phrase = any(title[start:start + len(requested)] == requested
                 for start in range(len(title) - len(requested) + 1))
    return round(100 * coverage + 20 * phrase + 10 * supporting, 4)
