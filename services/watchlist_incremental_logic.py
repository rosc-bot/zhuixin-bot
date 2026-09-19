"""Canonical helpers for title isolation and multi-episode batching."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)


def canonical_episode_keys(
    items: Iterable[object],
    *,
    default_season: int = 1,
) -> list[tuple[int, int]]:
    """Return a deduplicated, sorted list of canonical (season, episode) pairs."""
    result: set[tuple[int, int]] = set()
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                s_raw = item[0]
                e_raw = item[1]
                s = int(s_raw) if s_raw is not None else default_season
                e = int(e_raw)
                if s > 0 and e > 0:
                    result.add((s, e))
            except (TypeError, ValueError):
                continue
        elif isinstance(item, int) and item > 0:
            result.add((default_season, item))
    return sorted(result)


def title_scoped_text(text: str, title: str) -> str:
    """Return a scoped substring containing only the section for this title."""
    raw_text = str(text or "")
    normalized_title = "".join(
        char for char in str(title or "")
        if char.isalnum() or "\u4e00" <= char <= "\u9fff"
    )
    if not raw_text or not normalized_title:
        return raw_text

    title_pattern = r"[\W_]*".join(re.escape(char) for char in normalized_title)
    title_match = re.search(title_pattern, raw_text, flags=re.I)
    if not title_match:
        return raw_text

    tail = raw_text[title_match.end():]
    boundary = re.search(r"[\"“”‘’']|【", tail)
    segment_end = title_match.end() + boundary.start() if boundary else len(raw_text)
    segment = raw_text[title_match.start():segment_end].strip()
    return segment or raw_text


def title_scoped_urls(text: str, urls_json: str | None, title: str) -> list[str]:
    """Return URLs strictly belonging to the requested title in a post.

    Prevents cross-contamination where a post containing multiple series
    (e.g., 灵境行者 + 逆天邪神) leaks other series' share links into the target series.
    """
    raw_text = str(text or "")
    clean_title = re.sub(r'[^\w\u4e00-\u9fa5]', '', str(title or ""))
    if not raw_text or not clean_title:
        return []

    # 1. Collect all URLs
    all_urls: list[str] = []
    for match in re.finditer(r"https?://[^\s\"'<>]+", raw_text, flags=re.I):
        all_urls.append(match.group(0).rstrip(".,!?:;)]>"))
    if urls_json:
        try:
            data = json.loads(urls_json)
            values: list[object] = []
            if isinstance(data, dict):
                for key in ("all_urls", "text_urls", "button_urls", "entity_urls"):
                    values.extend(data.get(key) or [])
            elif isinstance(data, list):
                values.extend(data)
            all_urls.extend(
                str(v).strip().rstrip(".,!?:;)]>")
                for v in values
                if str(v).strip()
            )
        except Exception as json_err:
            logger.warning("title_scoped_urls: urls_json parse failed for title=%s: %s", title, json_err)
            # 兜底：即使 JSON 解析失败，正文里的 URL 也会被使用
    unique_all = list(dict.fromkeys(all_urls))
    if not unique_all:
        return []

    # If only 1 URL exists in total and title is present, safely return it
    if len(unique_all) == 1:
        clean_raw = re.sub(r'[^\w\u4e00-\u9fa5]', '', raw_text)
        if clean_title in clean_raw:
            return unique_all
        return []

    # 2. Multi-URL post: Segment strictly by items
    lines = raw_text.split('\n')
    entries: list[str] = []
    current_entry_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_entry_lines:
                current_entry_lines.append("")
            continue

        has_url = bool(re.search(r"https?://", stripped))
        prev_has_url = any(re.search(r"https?://", l) for l in current_entry_lines)

        if prev_has_url and not has_url:
            # We already have a link for the previous block, and now encountered new text line!
            entries.append("\n".join(current_entry_lines))
            current_entry_lines = [line]
        else:
            current_entry_lines.append(line)

    if current_entry_lines:
        entries.append("\n".join(current_entry_lines))

    title_urls: list[str] = []
    for entry in entries:
        clean_entry = re.sub(r'[^\w\u4e00-\u9fa5]', '', entry)
        if clean_title in clean_entry:
            urls_in_entry = [
                m.group(0).rstrip(".,!?:;)]>")
                for m in re.finditer(r"https?://[^\s\"'<>]+", entry, flags=re.I)
            ]
            title_urls.extend(urls_in_entry)

    return list(dict.fromkeys(title_urls))


def batch_fingerprint(keys: Iterable[object]) -> str:
    """Return a stable idempotency token for one canonical key set."""
    payload = json.dumps(
        [list(key) for key in canonical_episode_keys(keys)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "watchlist_" + hashlib.sha256(payload).hexdigest()[:20]
