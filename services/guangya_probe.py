"""Read-only Guangya share inspection for episode-level scout gating."""

import asyncio
import re
import time
import urllib.parse
from typing import Any, Dict, Optional, Set, Tuple

import aiohttp


VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".ts", ".iso", ".avi", ".mov", ".flv",
    ".wmv", ".m2ts", ".rmvb", ".webm",
}
CACHE_TTL = 300.0
MAX_FOLDERS = 30
MAX_PAGES_PER_FOLDER = 100

# url -> (timestamp, (season, episode) keys, set of filenames, or None when failed)
_PROBE_CACHE: Dict[
    str, Tuple[float, Optional[Set[Tuple[Optional[int], int]]], Set[str]]
] = {}


def _share_id_and_code(url: str) -> Tuple[Optional[str], str]:
    raw_url = str(url or "").strip()
    parsed = urllib.parse.urlsplit(raw_url)
    path = urllib.parse.unquote(parsed.path or "")
    match = re.search(r"/s/([a-zA-Z0-9_-]+)", path, re.I)
    if not match:
        return None, ""

    share_id = match.group(1).strip(" .")
    code = ""
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
    for key, values in query.items():
        if key.lower() in {"code", "pwd"} and values:
            code = values[0].strip()
            break
    if not code:
        code_match = re.search(
            r"(?:提取码|密码|码)[\s:：]*([a-zA-Z0-9]{4,8})", raw_url
        )
        if code_match:
            code = code_match.group(1)
    return share_id or None, code


def _api_ok(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    return code is None or str(code).strip() in {"0", "200"}


def _season_from_folder_name(name: str) -> Optional[int]:
    """Extract a season number from a directory name."""
    text = str(name or "")
    m = re.search(r"(?:第\s*([一二三四五六七八九十百0-9]+)\s*季|season\s*0*(\d+)|s0*(\d+))(?:$|[\s._-])", text, re.I)
    if not m:
        return None
    raw = next((x for x in m.groups() if x), None)
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    return {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}.get(raw)


def _extract_episode_keys(filename: str) -> Set[Tuple[Optional[int], int]]:
    name = str(filename or "")
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in VIDEO_EXTENSIONS:
        return set()

    keys: Set[Tuple[Optional[int], int]] = set()
    explicit_season_keys: Set[Tuple[Optional[int], int]] = set()
    for match in re.finditer(r"S(\d{1,4})\s*E(\d{1,4})", name, re.I):
        source_season = int(match.group(1))
        episode = int(match.group(2))
        if source_season < 1900 and 1 <= episode <= 2500:
            explicit_season_keys.add((source_season, episode))
    if explicit_season_keys:
        return explicit_season_keys

    for match in re.finditer(
        r"(?:^|[._\-\s\[【])(?:EP?|e)0*(\d{1,4})(?=$|[._\-\s\]】])", name, re.I
    ):
        episode = int(match.group(1))
        if 1 <= episode <= 2500:
            keys.add((None, episode))

    for match in re.finditer(r"(?:第\s*)?0*(\d{1,4})\s*(?:集|话)", name):
        episode = int(match.group(1))
        if 1 <= episode <= 2500:
            keys.add((None, episode))

    # 识别纯数字集数: 01-4K.mp4, 01.mp4, [01].mp4, 16-4K.高码率.mkv
    for match in re.finditer(r"(?:^|[._\-\s\[【])0*(\d{1,3})(?:v\d)?(?=[._\-\s\]】]|$)", name):
        val = match.group(1)
        if val in ("1080", "720", "2160", "480"):
            continue
        episode = int(val)
        if 1 <= episode <= 2500:
            keys.add((None, episode))

    return keys


async def _post_json(
    session: aiohttp.ClientSession,
    endpoint: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    for attempt in range(2):
        try:
            async with session.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as response:
                data = await response.json(content_type=None)
                if response.status == 200 and isinstance(data, dict):
                    return data
                if attempt == 0:
                    await asyncio.sleep(0.35)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            if attempt == 0:
                await asyncio.sleep(0.35)
    return None


async def probe_guangya_share(
    url: str,
    season: Optional[int] = None,
    title: Optional[str] = None,
    tmdb_id: Optional[int] = None,
    is_known_initial_url: bool = False,
) -> Optional[Set[int]]:
    """Return real episode numbers in a public share, or None if unavailable.

    If ``title`` is provided, ensures the share actually belongs to the intended show,
    preventing multi-resource telegram post cross-linking.
    """
    clean_url = str(url or "").strip()
    if not clean_url:
        return None
    now = time.monotonic()
    cached = _PROBE_CACHE.get(clean_url)
    
    clean_title = re.sub(r'[^\w\u4e00-\u9fa5]', '', str(title or "")) if title else ""

    if cached and now - cached[0] < CACHE_TTL:
        raw_keys = cached[1]
        all_names = cached[2]
        if raw_keys is None:
            return None
        # Verify title match if title provided
        if clean_title and not is_known_initial_url:
            names_text = re.sub(r'[^\w\u4e00-\u9fa5]', '', "".join(all_names))
            all_raw = ' '.join(all_names).lower()
            tmdb_match = bool(tmdb_id and (f'tmdb-{tmdb_id}' in all_raw or f'tmdbid-{tmdb_id}' in all_raw))
            if names_text and clean_title not in names_text and not tmdb_match:
                return set()  # Title completely mismatched!
        return {
            episode for source_season, episode in raw_keys
            if season is None or source_season is None or source_season == season
        }

    share_id, code = _share_id_and_code(clean_url)
    if not share_id:
        _PROBE_CACHE[clean_url] = (now, None, set())
        return None

    headers = {
        "Content-Type": "application/json",
        "Origin": "https://www.guangyapan.com",
        "Referer": "https://www.guangyapan.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
    }
    summary_endpoint = "https://api.guangyapan.com/userres/v1/get_share_summary"
    token_endpoint = "https://api.guangyapan.com/userres/v1/get_share_access_token"
    files_endpoint = "https://api.guangyapan.com/userres/v1/get_share_page_files_list"

    episode_keys: Set[Tuple[Optional[int], int]] = set()
    found_names: Set[str] = set()

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
            summary = await _post_json(
                session, summary_endpoint, {"shareId": share_id}, headers
            )
            if not summary or not _api_ok(summary) or not isinstance(summary.get("data"), dict):
                _PROBE_CACHE[clean_url] = (time.monotonic(), None, set())
                return None

            share_title = (
                summary.get("data", {}).get("shareTitle")
                or summary.get("data", {}).get("title")
                or ""
            )
            if share_title:
                found_names.add(share_title)

            token_result = await _post_json(
                session,
                token_endpoint,
                {"shareId": share_id, "code": code},
                headers,
            )
            if not token_result or not _api_ok(token_result) or not isinstance(token_result.get("data"), dict):
                _PROBE_CACHE[clean_url] = (time.monotonic(), None, set())
                return None
            access_token = token_result["data"].get("accessToken") or ""
            if not access_token:
                _PROBE_CACHE[clean_url] = (time.monotonic(), None, set())
                return None

            auth_headers = {**headers, "authorization": f"Bearer {access_token}"}
            # Preserve Season N context: many shares use filenames like 01.mkv.
            queue = [("", None)]
            visited: Set[str] = set()
            while queue and len(visited) < MAX_FOLDERS:
                parent_id, inherited_season = queue.pop(0)
                if parent_id in visited:
                    continue
                visited.add(parent_id)
                page = 0
                collected = 0
                while page <= MAX_PAGES_PER_FOLDER:
                    payload: Dict[str, Any] = {
                        "shareId": share_id,
                        "page": page,
                        "pageSize": 100,
                        "accessToken": access_token,
                        "orderBy": 0,
                        "sortType": 0,
                    }
                    if parent_id:
                        payload["parentId"] = parent_id
                    result = await _post_json(session, files_endpoint, payload, auth_headers)
                    if not result or not _api_ok(result):
                        _PROBE_CACHE[clean_url] = (time.monotonic(), None, set())
                        return None
                    data = result.get("data") or {}
                    items = data.get("list") or []
                    if not items:
                        break
                    collected += len(items)
                    for item in items:
                        fn = str(item.get("fileName") or "")
                        if fn:
                            found_names.add(fn)
                        if str(item.get("resType")) == "2":
                            folder_id = item.get("fileId")
                            if folder_id:
                                folder_season = _season_from_folder_name(fn) or inherited_season
                                queue.append((str(folder_id), folder_season))
                        else:
                            file_keys = _extract_episode_keys(fn)
                            if inherited_season is not None:
                                file_keys = {
                                    (inherited_season if source_season is None else source_season, episode)
                                    for source_season, episode in file_keys
                                }
                            episode_keys.update(file_keys)

                    total_value = data.get("total", result.get("total"))
                    try:
                        total = int(total_value) if total_value is not None else None
                    except (TypeError, ValueError):
                        total = None
                    if (total is not None and collected >= total) or len(items) < 100:
                        break
                    page += 1

    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
        _PROBE_CACHE[clean_url] = (time.monotonic(), None, set())
        return None

    _PROBE_CACHE[clean_url] = (time.monotonic(), set(episode_keys), found_names)

    # Title gate
    if clean_title and not is_known_initial_url:
        names_text = re.sub(r'[^\w\u4e00-\u9fa5]', '', "".join(found_names))
        all_raw = ' '.join(found_names).lower()
        tmdb_match = bool(tmdb_id and (f'tmdb-{tmdb_id}' in all_raw or f'tmdbid-{tmdb_id}' in all_raw))
        if names_text and clean_title not in names_text and not tmdb_match:
            return set()  # Not this show!

    return {
        episode for source_season, episode in episode_keys
        if season is None or source_season is None or source_season == season
    }
