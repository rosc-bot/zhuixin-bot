from services.framehdr_service import FrameHdrService
import time
from services.library_service import LibraryService
import re
import json
import sqlite3
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from html import escape
from typing import Any, Dict, List, Optional, Tuple
import aiohttp
from config import (
    TG_MESSAGES_DB_PATHS,
    INGEST_PUSH_URL,
    INGEST_PUSH_TOKEN,
    INGEST_CHANNEL_ID,
    ADMIN_TG_ID,
)
from database import DatabaseService
from services.calendar_service import CalendarService
from services.guangya_probe import probe_guangya_share
from services.watchlist_incremental_logic import title_scoped_text, title_scoped_urls

logger = logging.getLogger(__name__)

TARGET_SCOUT_CHANNEL_IDS = [
    -1003808659413,  # @regengguangya 光鸭云盘影视热更频道
    -1003974900477,  # @qiqiwangpan 剧开心
    -1004435637826,  # @guangya_hdhive 光鸭云盘资源收藏
    -1003667471790,  # @pan_guangya 光鸭云盘资源频道
    -1003702243011,  # @guangyapan1 光鸭云盘资源分享群
    -1004429917555,  # @guangyayunpan 光鸭云盘资源频道
]
TARGET_SCOUT_USERNAMES = ['regengguangya', 'qiqiwangpan', 'guangya_hdhive', 'pan_guangya', 'guangyapan1', 'guangyayunpan']

CLOUD_DOMAINS = [
    ("guangya", r"(?:pan\.)?(?:guangyapan|gypan)\.com/s/([a-zA-Z0-9_-]+)"),
    ("quark", r"pan\.quark\.cn/s/([a-zA-Z0-9_-]+)"),
    ("aliyun", r"(?:www\.)?(?:alipan\.com|aliyundrive\.com)/s/([a-zA-Z0-9_-]+)"),
    ("115", r"115\.com/s/([a-zA-Z0-9_-]+)"),
    ("baidu", r"pan\.baidu\.com/s/([a-zA-Z0-9_-]+)"),
    ("tianyi", r"cloud\.189\.cn/(?:t/|web/share\?code=)([a-zA-Z0-9_-]+)"),
]

class ScoutService:
    @staticmethod
    def get_db_path() -> Optional[str]:
        import os
        for p in TG_MESSAGES_DB_PATHS:
            if os.path.exists(p):
                return p
        return None

    @classmethod
    def extract_cloud_links(cls, text: str, urls_json: Optional[str] = None) -> List[Dict[str, str]]:
        found = []
        seen = set()

        all_candidate_urls = []
        if urls_json:
            try:
                data = json.loads(urls_json)
                if isinstance(data, dict):
                    for k in ("all_urls", "text_urls", "button_urls"):
                        all_candidate_urls.extend(data.get(k) or [])
                elif isinstance(data, list):
                    all_candidate_urls.extend(data)
            except Exception:
                pass

        for url in re.findall(r'https?://[^\s"\'<>]+', text or ""):
            all_candidate_urls.append(url)

        provider_order = {"guangya": 1, "quark": 2, "aliyun": 3, "115": 4, "baidu": 5, "tianyi": 6, "xunlei": 7, "uc": 8}
        for u in all_candidate_urls:
            clean_u = u.strip().rstrip(".,!?:;)]>")
            for provider, pattern in CLOUD_DOMAINS:
                if re.search(pattern, clean_u, re.I):
                    if clean_u not in seen:
                        seen.add(clean_u)
                        found.append({"provider": provider, "url": clean_u, "prio": provider_order.get(provider, 99)})
                    break
        found.sort(key=lambda x: x["prio"])
        return [{"provider": x["provider"], "url": x["url"]} for x in found]

    @classmethod
    def scout_messages_for_series(
        cls,
        title: str,
        season: int = 1,
        episodes: Optional[List[int]] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        db_path = cls.get_db_path()
        if not db_path:
            logger.warning("tg_messages.db not found!")
            return []

        clean_title = re.sub(r'[^\w\u4e00-\u9fa5]', '', title)
        if not clean_title:
            return []

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        c = conn.cursor()

        query = """
            SELECT id, chat_title, text, urls, date, chat_id
            FROM messages
            WHERE chat_id IN (-1003808659413, -1003974900477, -1004435637826, -1003667471790, -1003702243011, -1004429917555)
              AND text LIKE ?
            ORDER BY id DESC
            LIMIT 100
        """
        rows = c.execute(query, (f"%{clean_title}%",)).fetchall()
        conn.close()

        results = []
        cst = timezone(timedelta(hours=8))
        target_ep_set = set(episodes or [])

        for row in rows:
            msg_id, chat_title, text, urls_json, date_val = row[0], row[1], row[2], row[3], row[4]
            text_str = text or ""
            links = cls.extract_cloud_links(text_str, urls_json)
            if not links:
                continue
            scoped_urls = set(title_scoped_urls(text_str, urls_json, title))
            if scoped_urls:
                links = [link for link in links if link.get("url") in scoped_urls]
            elif len(links) != 1:
                logger.info(
                    "Skipping ambiguous multi-link post for %s: message=%s link_count=%s",
                    title,
                    msg_id,
                    len(links),
                )
                continue
            if not links:
                continue

            # Enhanced episode extraction from text (ranges, updates, single tokens)
            extracted_post_eps = set()
            # 1. Ranges: E01-E03, EP01-EP10, 01-03集, 01~03
            for m in re.finditer(r'(?:[Ee]|EP|ep)?\s*0*(\d{1,4})\s*(?:-|~|到|至)\s*(?:[Ee]|EP|ep)?\s*0*(\d{1,4})\s*(?:集|话)?', text_str):
                s_ep, e_ep = int(m.group(1)), int(m.group(2))
                if 1 <= s_ep <= e_ep <= 2500 and (e_ep - s_ep) <= 150:
                    extracted_post_eps.update(range(s_ep, e_ep + 1))

            # 2. "更至03集", "更新至03集", "更新到03集", "全03集" -> 1..3
            for m in re.finditer(r'(?:更至|更新至|更新到|更新|全)\s*0*(\d{1,4})\s*(?:集|话)?', text_str):
                max_ep = int(m.group(1))
                if 1 <= max_ep <= 2500:
                    extracted_post_eps.update(range(1, max_ep + 1))

            # 3. Single tokens: E03, EP03, 第03集
            for m in re.finditer(r'(?:[Ee]|EP|ep)\s*0*(\d{1,4})\b', text_str):
                val = int(m.group(1))
                if 1 <= val <= 2500 and val not in (1080, 2160, 720, 2020, 2021, 2022, 2023, 2024, 2025, 2026):
                    extracted_post_eps.add(val)
            for m in re.finditer(r'第\s*0*(\d{1,4})\s*(?:集|话)', text_str):
                val = int(m.group(1))
                if 1 <= val <= 2500:
                    extracted_post_eps.add(val)

            matched_eps = sorted(target_ep_set & extracted_post_eps) if target_ep_set else sorted(extracted_post_eps)

            date_cst_str = ""
            if date_val:
                try:
                    dt = datetime.fromtimestamp(float(date_val), tz=timezone.utc).astimezone(cst)
                    date_cst_str = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass

            for link in links:
                results.append({
                    "msg_id": msg_id,
                    "chat_title": chat_title or "群聊分享",
                    "title": title,
                    "season": season,
                    "provider": link["provider"],
                    "url": link["url"],
                    "matched_episodes": matched_eps,
                    "date_cst": date_cst_str,
                    "snippet": (text_str[:120].replace("\n", " ") + "...") if len(text_str) > 120 else text_str,
                })
                if len(results) >= limit:
                    continue

        deduped: List[Dict[str, Any]] = []
        by_url: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for item in results:
            key = (item["provider"], item["url"].rstrip("/"))
            existing = by_url.get(key)
            if existing is not None:
                existing["matched_episodes"] = sorted(
                    set(existing.get("matched_episodes") or [])
                    | set(item.get("matched_episodes") or [])
                )
                continue
            by_url[key] = item
            deduped.append(item)
        return deduped[:limit]

    @classmethod
    async def filter_candidates_by_real_episodes(
        cls,
        candidates: List[Dict[str, Any]],
        target_episodes: List[int],
    ) -> List[Dict[str, Any]]:
        """Apply a real-file gate before a share can be sent to the ingest bot."""
        target_set = set(target_episodes or [])
        if not target_set:
            return []

        verified: List[Dict[str, Any]] = []
        for candidate in candidates:
            provider = candidate.get("provider")
            if provider == "guangya":
                actual = await probe_guangya_share(
                    candidate.get("url", ""),
                    season=int(candidate.get("season") or 1),
                    title=candidate.get("title"),
                    tmdb_id=candidate.get("tmdb_id"),
                    is_known_initial_url=bool(candidate.get("is_initial_url")),
                )
                if actual is None:
                    logger.warning("Unable to verify Guangya share; skipping candidate: %s", candidate.get("url"))
                    continue
                matched = sorted(target_set & actual)
                if not matched:
                    continue
                checked = dict(candidate)
                checked["actual_episodes"] = sorted(actual)
                checked["matched_episodes"] = matched
                verified.append(checked)
            elif set(candidate.get("matched_episodes") or []) & target_set:
                verified.append(candidate)
        return verified

    @classmethod
    async def push_to_tg_media_bot(
        cls,
        title: str,
        season: int,
        episodes: List[int],
        share_url: str,
        text_context: str = "",
        tmdb_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Pushes scouted resource into tg-media-bot via internal ingest API."""
        if not INGEST_PUSH_URL or not INGEST_PUSH_TOKEN:
            return {"success": False, "error": "未配置内部推送接口 (INGEST_PUSH_URL / INGEST_PUSH_TOKEN)"}

        ep_str = " ".join(f"E{e:02d}" for e in episodes) if episodes else "全集"
        tmdb_anchor = f"https://www.themoviedb.org/tv/{int(tmdb_id)}" if tmdb_id else ""
        synthetic_text = f"{title} S{season:02d} {ep_str}\n{tmdb_anchor}\n{share_url}\n{text_context}"

        payload = {
            "channel_id": INGEST_CHANNEL_ID,
            "message_id": int(time.time() * 1000) % 2000000000,
            "text": synthetic_text,
            "source_type": "watchlist_scout",
        }
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Token": INGEST_PUSH_TOKEN,
        }

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
                async with session.post(INGEST_PUSH_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    try:
                        data = await resp.json()
                    except Exception:
                        data = {"raw_text": await resp.text()}
                    logger.info("Ingest push result (HTTP %s): %s", resp.status, data)
                    if resp.status in (200, 201):
                        return {"success": True, "data": data}
                    else:
                        detail = data.get("detail") or data.get("message") or data.get("error") or str(data)
                        return {"success": False, "error": f"HTTP {resp.status}: {detail}", "data": data}
        except Exception as e:
            logger.warning("Ingest push failed: %s", e)
            return {"success": False, "error": str(e)}

    @classmethod
    async def scout_missing_episodes_for_sub(
        cls,
        sub: Dict[str, Any],
        bot: Optional[Any] = None,
        notify_on_empty: bool = False
    ) -> Dict[str, Any]:
        """
        Scouts missing episodes for a given series subscription, respecting follow_mode.
        """
        title = sub.get("title")
        season = sub.get("season") or 1
        sub_id = sub.get("id")
        follow_mode = sub.get("follow_mode") or "LATEST"
        if not title:
            return {"status": "not_found", "message": "无效剧名"}

        # Check proactive ignored rules
        ignored_rules = await DatabaseService.get_ignored_rules()
        clean_title = re.sub(r'[^\w\u4e00-\u9fa5]', '', title)
        rules = ignored_rules.get((title, season), set()) | ignored_rules.get((clean_title, season), set())
        if 0 in rules:
            return {"status": "ignored", "message": f"《{title}》第 {season} 季已主动关闭缺集提醒与自动打捞。"}

        raw_coll = sub.get("collected_episodes") or []
        if isinstance(raw_coll, str):
            import json
            try: raw_coll = json.loads(raw_coll)
            except: raw_coll = []
        collected = {int(x) for x in raw_coll if str(x).isdigit()}
        max_coll = max(collected) if collected else 0

        # Calculate target missing episodes respecting follow_mode
        if sub.get("target_eps"):
            target_eps = list(sub["target_eps"])
        else:
            try: last_aired = int(sub.get("last_aired_episode") or 0)
            except: last_aired = 0

            target_eps = []
            if follow_mode == "LATEST":
                if last_aired > max_coll:
                    target_eps = [e for e in range(max_coll + 1, last_aired + 1)]
                elif max_coll > 0:
                    target_eps = [max_coll + 1]
                else:
                    target_eps = [last_aired] if last_aired > 0 else [1]
            else:
                total = sub.get("total_episodes") or max(last_aired, max_coll, 1)
                target_eps = [e for e in range(1, total + 1) if e not in collected]

        # Reconcile stale watchlist counters with the real transferred library.
        stored_eps = set(collected)
        try:
            library_entries = await LibraryService.get_transferred_library()
            library_entry = LibraryService.match_show_in_library(library_entries, title, season)
        except Exception as reconcile_error:
            logger.warning("Watchlist/library reconciliation failed for %s S%02d: %s", title, season, reconcile_error)
            library_entry = None
        if library_entry:
            library_eps = {
                int(ep) for ep in (library_entry.get("episodes") or [])
                if str(ep).strip().isdigit()
            }
            new_eps = sorted(library_eps - stored_eps)
            if new_eps:
                await DatabaseService.mark_collected(sub_id, new_eps)
                stored_eps.update(new_eps)
                sub["collected_episodes"] = sorted(stored_eps)
                logger.info(
                    "Reconciled watchlist %s S%02d from library: added collected episodes=%s",
                    title, season, new_eps,
                )
            max_library_ep = max(library_eps) if library_eps else 0
            if max_library_ep > int(sub.get("last_aired_episode") or 0):
                sub["last_aired_episode"] = max_library_ep

        # Recalculate the LATEST target after reconciliation.
        if follow_mode == "LATEST":
            max_collected = max(stored_eps, default=0)
            last_aired = max(int(sub.get("last_aired_episode") or 0), max_collected)
            target_eps = [max_collected + 1] if max_collected else ([last_aired] if last_aired else [1])
        elif not sub.get("target_eps"):
            total = sub.get("total_episodes") or max(int(sub.get("last_aired_episode") or 0), 1)
            raw_coll = sub.get("collected_episodes") or []
            if isinstance(raw_coll, str):
                import json
                try: raw_coll = json.loads(raw_coll)
                except: raw_coll = []
            collected = {int(x) for x in raw_coll if str(x).isdigit()}
            target_eps = [e for e in range(1, total + 1) if e not in collected]
        target_eps = [e for e in target_eps if e not in rules]
        if not target_eps:
            return {"status": "ignored", "message": f"《{title}》第 {season} 季当前缺集已被主动忽略，跳过打捞。"}

        scouted = await asyncio.to_thread(
            cls.scout_messages_for_series,
            title=title,
            season=season,
            episodes=target_eps,
            limit=5
        )

        # 🌟 接入 FrameHdr 帧影资源站精确搜索打捞
        try:
            fh_results = await FrameHdrService.search_series(
                title=title,
                season=season,
                episodes=target_eps,
                tmdb_id=sub.get("tmdb_id"),
                limit=3
            )
            if fh_results:
                for fhr in fh_results:
                    if not any(c.get("url") == fhr["url"] for c in scouted):
                        scouted.append(fhr)
                logger.info("FrameHdr provided %d candidates for %s S%02d", len(fh_results), title, season)
        except Exception as e_fh:
            logger.warning("FrameHdr search failed for %s S%02d: %s", title, season, e_fh)

        # 🌟 优先获取该剧在转存库中的最初首发分享链接（Initial Share URL）
        try:
            initial_share_url = await LibraryService.get_initial_share_url(title, season)
            if initial_share_url:
                scouted.insert(0, {
                    "provider": "guangya" if "guangya" in initial_share_url else "quark",
                    "url": initial_share_url,
                    "title": title,
                    "season": season,
                    "date": time.time(),
                    "msg_id": 9999999999,
                    "chat_title": "最初首发源优先锁定",
                    "snippet": f"{title} S{season:02d} 最初首发源优先锁定",
                    "is_initial_url": True,
                })
                logger.info("Prioritized initial share URL for %s S%02d: %s", title, season, initial_share_url)
        except Exception as e_init:
            logger.warning("Failed to fetch initial share URL for %s S%02d: %s", title, season, e_init)

        # 🌟 补充历史转存过的网盘原链接
        try:
            historical_urls = await DatabaseService.get_historical_share_urls(title, season)
            for h_url in historical_urls:
                if not any(c.get("url") == h_url for c in scouted):
                    scouted.insert(0, {
                        "provider": "guangya" if "guangya" in h_url else "quark",
                        "url": h_url,
                        "title": title,
                        "season": season,
                        "date": time.time(),
                        "msg_id": 999999999,
                        "chat_title": "历史原链接巡更",
                        "snippet": f"{title} S{season:02d} 历史原链接自动巡更",
                    })
        except Exception as e_h:
            logger.warning("Failed to fetch historical share urls for %s: %s", title, e_h)

        if not scouted:
            logger.info("No scouted resources found for %s S%02d target eps=%s", title, season, target_eps)
            return {
                "status": "not_found",
                "message": f"在锁定影视频道/群聊历史中暂未检索到《{title}》新分享链接，雷达将在后台持续监控",
            }

        scouted = await cls.filter_candidates_by_real_episodes(scouted, target_eps)
        if not scouted:
            t_str = ",".join(f"E{e}" for e in target_eps)
            logger.info("Scouted links failed real-file episode gate for %s target eps=%s", title, target_eps)
            return {
                "status": "not_found",
                "message": f"频道中虽有《{title}》分享，但真实文件未包含目标缺集（待补: {t_str}），已拦截不盲转",
            }

        provider_prio = {"guangya": 1, "quark": 2, "aliyun": 3, "115": 4, "baidu": 5, "tianyi": 6}
        matched_candidates = [c for c in scouted if c.get("matched_episodes")]
        if not matched_candidates:
            logger.info("Scouted messages found for %s, but NONE contained target episodes %s", title, target_eps)
            t_str = ",".join(f"E{e}" for e in target_eps)
            return {
                "status": "not_found",
                "message": f"频道中虽有《{title}》的历史分享，但尚未发布目标缺集（待补: {t_str}），已自动拦截不盲转",
            }

        # 爸爸偏好：Gyy 优质发布者置顶优先，其次是最初首发源，再次是缺集覆盖度和光鸭
        def _get_cand_prio(cand):
            snippet = str(cand.get("snippet") or "")
            chat_title = str(cand.get("chat_title") or "")
            is_gyy = 0 if ("Gyy" in snippet or "Gyy" in chat_title) else 1
            is_init = 0 if cand.get("is_initial_url") else 1
            return (
                is_gyy,
                is_init,
                -len(cand.get("matched_episodes", [])),
                provider_prio.get(cand.get("provider"), 99),
                -int(cand.get("msg_id") or 0)
            )

        matched_candidates.sort(key=_get_cand_prio)

        last_outcome = None
        for cand_idx, candidate in enumerate(matched_candidates):
            raw_matched = candidate.get("matched_episodes") or target_eps
            eps_to_push = sorted(set(raw_matched) & set(target_eps)) if target_eps else list(raw_matched)
            if not eps_to_push:
                continue

            is_init = candidate.get("is_initial_url")
            logger.info("Evaluating candidate [%d/%d] (%s, init=%s) for %s: %s",
                        cand_idx + 1, len(matched_candidates), candidate.get("provider"), is_init, title, candidate.get("url"))

            push_res = await cls.push_to_tg_media_bot(
                title=title,
                season=season,
                episodes=eps_to_push,
                share_url=candidate["url"],
                text_context=candidate.get("snippet", ""),
                tmdb_id=sub.get("tmdb_id") or candidate.get("tmdb_id")
            )

            if not push_res.get("success"):
                err_msg = push_res.get("error") or "推送转存服务异常"
                logger.warning("Scout push failed for %s candidate %s: %s", title, candidate["url"], err_msg)
                last_outcome = {
                    "status": "push_failed",
                    "message": f"打捞到链接但推送转存失败: {err_msg}",
                    "best_candidate": candidate,
                    "error": err_msg,
                }
                continue

            data = push_res.get("data") or {}
            st_val = data.get("status")
            if st_val in ("skipped", "ignored"):
                reason = data.get("reason", "unknown")
                logger.warning("Ingest push was %s: reason=%s data=%s", st_val, reason, data)
                last_outcome = {
                    "status": "push_failed",
                    "message": f"转存服务跳过/忽略该链接: {reason}",
                    "best_candidate": candidate,
                    "error": reason,
                }
                continue

            dedups = data.get("deduplications") or []
            jobs = data.get("jobs") or []

            # Check deduplication status
            if dedups and not jobs:
                act = dedups[0].get("action")
                reason = dedups[0].get("reason", "duplicate")
                if act not in ("requeued", "incremental_requeued"):
                    logger.info("Candidate %s deduplicated without new jobs (act=%s, reason=%s), trying next fallback...",
                                candidate["url"], act, reason)
                    last_outcome = {
                        "status": "already_ingested",
                        "message": f"该资源链接此前已收录过（防重过滤: {reason}），无需重复录入",
                        "best_candidate": candidate,
                    }
                    continue

            if not jobs and not dedups:
                logger.warning("Ingest push returned neither jobs nor dedups: %s", data)
                last_outcome = {
                    "status": "push_failed",
                    "message": "转存服务未生成有效任务队列",
                    "best_candidate": candidate,
                    "error": "转存服务未生成有效任务队列",
                }
                continue

            return {
                "status": "found",
                "message": f"成功命中网盘资源并送入转存队列！",
                "best_candidate": candidate,
                "pushed_episodes": eps_to_push,
            }

        return last_outcome or {"status": "not_found", "message": f"《{title}》所有候选链接均未生成有效转存任务"}

    @classmethod
    async def sync_and_scout_all(
        cls,
        bot: Optional[Any] = None,
        progress_callback: Optional[Any] = None
    ) -> Dict[str, Any]:
        today_data = await CalendarService.get_today_shows("domestic")
        calendar_shows = today_data.get("shows", [])

        # Auto-refresh radar and sync any newly transferred ongoing shows into watchlist
        try:
            await LibraryService.get_radar_summary(calendar_shows, follow_mode="LATEST", force_refresh=True)
        except Exception as rad_e:
            logger.warning("Error refreshing radar in sync_and_scout_all: %s", rad_e)

        subs = await DatabaseService.list_subscriptions()
        active_subs = [s for s in subs if s.get("status") == "FOLLOWING"]

        pushed_count = 0
        skipped_duplicates = 0
        pushed_shows = []
        total_subs = len(active_subs)
        done_count = 0
        sem = asyncio.Semaphore(4)
        lock = asyncio.Lock()

        async def _scout_worker(sub_item):
            nonlocal pushed_count, skipped_duplicates, done_count
            sub_title = sub_item.get("title") or "未知"
            res_dict = {}
            try:
                async with sem:
                    res_dict = await cls.scout_missing_episodes_for_sub(sub_item, bot=bot)
            except Exception as sub_err:
                logger.warning("Error scouting sub %s: %s", sub_title, sub_err)
                res_dict = {"status": "error", "message": str(sub_err)}

            async with lock:
                done_count += 1
                st = res_dict.get("status")
                if st == "found":
                    pushed_count += 1
                    p_eps = res_dict.get("pushed_episodes") or []
                    pushed_shows.append({
                        "title": sub_title,
                        "season": sub_item.get("season", 1),
                        "episodes": p_eps
                    })
                elif st == "already_ingested":
                    skipped_duplicates += 1

                if progress_callback:
                    try:
                        await progress_callback(done_count, total_subs, sub_title, pushed_count)
                    except Exception:
                        pass
            return res_dict

        if active_subs:
            await asyncio.gather(*[_scout_worker(s) for s in active_subs], return_exceptions=True)

        return {
            "calendar_shows_count": len(calendar_shows),
            "subs_count": len(active_subs),
            "pushed_count": pushed_count,
            "skipped_duplicates": skipped_duplicates,
            "pushed_shows": pushed_shows,
        }

    @classmethod
    async def auto_ingest_hot_calendar_shows(cls, bot: Optional[Any] = None) -> Dict[str, Any]:
        """
        Automatically inspects today's hot broadcast shows from Calendar (domestic, anime, western, etc.).
        If a show is not in library or has new episodes aired today, automatically scouts the 4 target
        channels for share links (guangya preferred) and submits them for transfer!
        """
        if not await DatabaseService.is_auto_ingest_enabled():
            logger.info("Auto-ingest is disabled by user settings.")
            return {"status": "disabled", "pushed_count": 0}

        active_cats = await DatabaseService.get_auto_ingest_categories()
        library = await LibraryService.get_transferred_library()
        ignored_rules = await DatabaseService.get_ignored_rules()

        pushed_shows = []
        skipped_shows = []
        total_scanned = 0

        for cat_key in active_cats:
            try:
                cat_data = await CalendarService.get_shows_for_category_and_date(cat_key, date_offset=0)
                shows = cat_data.get("shows", [])
                cat_name = cat_data.get("cat_name") or cat_key
                for s in shows:
                    title = s["title"]
                    sea = s.get("season", 1)
                    eps = s.get("episodes") or []
                    if not eps:
                        continue
                    total_scanned += 1

                    # Check ignored rules
                    clean_t = re.sub(r'[^\w\u4e00-\u9fa5]', '', title)
                    rules = ignored_rules.get((title, sea), set()) | ignored_rules.get((clean_t, sea), set())
                    if 0 in rules:
                        continue

                    # Match with library
                    match = LibraryService.match_show_in_library(library, title, sea)
                    eval_res = await LibraryService.evaluate_item_status(match, eps, follow_mode="LATEST")

                    if eval_res.get("in_library") and eval_res.get("action_type") == "completed":
                        continue

                    target_eps = eval_res.get("missing_episodes") or eps
                    target_eps = [e for e in target_eps if e not in rules]
                    if not target_eps:
                        continue

                    scouted = []
                    # 🌟 优先获取该剧在转存库中的最初首发分享链接（Initial Share URL）
                    try:
                        initial_share_url = await LibraryService.get_initial_share_url(title, sea)
                        if initial_share_url:
                            scouted.append({
                                "provider": "guangya" if "guangya" in initial_share_url else "quark",
                                "url": initial_share_url,
                                "title": title,
                                "season": sea,
                                "date": time.time(),
                                "msg_id": 9999999999,
                                "chat_title": "最初首发源优先锁定",
                                "snippet": f"{title} S{sea:02d} 最初首发源优先锁定",
                                "is_initial_url": True,
                            })
                            logger.info("Auto-ingest prioritized initial share URL for %s S%02d: %s", title, sea, initial_share_url)
                    except Exception as e_init_auto:
                        logger.warning("Auto-ingest initial share URL fetch failed for %s S%02d: %s", title, sea, e_init_auto)

                    scouted_msgs = await asyncio.to_thread(
                        cls.scout_messages_for_series,
                        title=title,
                        season=sea,
                        episodes=target_eps,
                        limit=3
                    )
                    scouted.extend(scouted_msgs)
                    # 🌟 补充 FrameHdr 帧影每日排期或精确匹配资源
                    try:
                        fh_cand = await FrameHdrService.search_series(
                            title=title,
                            season=sea,
                            episodes=target_eps,
                            tmdb_id=s.get("tmdb_id"),
                            limit=2
                        )
                        if fh_cand:
                            for f_c in fh_cand:
                                if not any(c.get("url") == f_c["url"] for c in scouted):
                                    scouted.append(f_c)
                    except Exception as e_fh_auto:
                        logger.warning("FrameHdr auto-ingest candidate fetch failed for %s: %s", title, e_fh_auto)
                    if not scouted:
                        continue

                    scouted = await cls.filter_candidates_by_real_episodes(scouted, target_eps)
                    if not scouted:
                        logger.info("Scouted links failed real-file episode gate for %s target eps=%s", title, target_eps)
                        continue

                    provider_prio = {"guangya": 1, "quark": 2, "aliyun": 3, "115": 4, "baidu": 5, "tianyi": 6}
                    matched_candidates = [c for c in scouted if any(ep in c.get("matched_episodes", []) for ep in target_eps)]
                    if not matched_candidates:
                        logger.info("Found %s posts for %s, but NONE contained target eps %s. Skipping!", len(scouted), title, target_eps)
                        continue

                    matched_candidates.sort(key=lambda x: (
                        0 if x.get("is_initial_url") else 1,
                        -len(set(x.get("matched_episodes", [])) & set(target_eps)),
                        provider_prio.get(x.get("provider"), 99),
                        -int(x.get("msg_id") or 0)
                    ))
                    best_candidate = matched_candidates[0]
                    cand_url = best_candidate.get("url") or ""
                    cand_prov = best_candidate.get("provider") or "unknown"

                    raw_matched = best_candidate.get("matched_episodes") or target_eps
                    push_eps = sorted(set(raw_matched) & set(target_eps)) if target_eps else list(raw_matched)
                    if not push_eps:
                        logger.info("Best candidate for %s matched eps %s, but none in target %s - skipping to avoid pulling unrelated episodes", title, raw_matched, target_eps)
                        continue

                    if await DatabaseService.is_auto_ingested(title, sea, cand_url, push_eps):
                        logger.info("Candidate %s for %s eps %s already ingested, skipping", cand_url, title, push_eps)
                        continue
                    push_res = await cls.push_to_tg_media_bot(
                        title=title,
                        season=sea,
                        episodes=push_eps,
                        share_url=cand_url,
                        text_context=best_candidate.get("snippet", ""),
                        tmdb_id=s.get("tmdb_id") or best_candidate.get("tmdb_id")
                    )

                    if push_res.get("success"):
                        data = push_res.get("data") or {}
                        dedups = data.get("deduplications") or []
                        jobs = data.get("jobs") or []
                        
                        await DatabaseService.record_auto_ingest(
                            title, sea, push_eps, cand_url, cand_prov
                        )

                        if dedups and not jobs:
                            act = dedups[0].get("action")
                            if act != "requeued":
                                skipped_shows.append(title)
                                continue

                        # 推入新资源后主动失效雷达缓存，避免用户看到过期快照
                        try:
                            # from library_service import LibraryService
                            LibraryService.invalidate_radar_cache()
                        except Exception as cache_e:
                            logger.warning("invalidate_radar_cache failed after ingest %s: %s", title, cache_e)

                        pushed_shows.append({"title": title, "season": sea, "episodes": push_eps, "provider": cand_prov})
                    else:
                        err_msg = push_res.get("error") or "推送转存服务异常"
                        push_id = await DatabaseService.record_failed_scout_push(
                            title=title,
                            season=sea,
                            episodes=push_eps,
                            share_url=cand_url,
                            provider=cand_prov,
                            text_context=best_candidate.get("snippet", ""),
                            error_message=err_msg,
                        )
                        if bot and ADMIN_TG_ID:
                            try:
                                from live_status_monitor import format_failure_reason
                                from aiogram import types
                                from aiogram.utils.keyboard import InlineKeyboardBuilder

                                f_info = format_failure_reason(err_msg)
                                safe_title = escape(str(title))
                                safe_url = escape(str(cand_url))
                                safe_prov = escape(str(cand_prov))
                                safe_sum = escape(f_info["summary"])
                                safe_detail = escape(f_info["detail"])
                                safe_sol = escape(f_info["solution"])
                                safe_raw = escape(f_info["raw"])

                                builder = InlineKeyboardBuilder()
                                builder.row(
                                    types.InlineKeyboardButton(text="🔄 重试推送", callback_data=f"tx_sp_retry:{push_id}"),
                                    types.InlineKeyboardButton(text="🛑 取消任务", callback_data=f"tx_sp_cancel:{push_id}"),
                                )
                                builder.row(
                                    types.InlineKeyboardButton(text="🔍 重新打捞", callback_data=f"tx_sp_rescout:{push_id}"),
                                    types.InlineKeyboardButton(text="🚫 忽略此剧", callback_data=f"tx_sp_ignore:{push_id}"),
                                )

                                logger.info("Skip zhuixin-bot push (unified to tg-media-bot)")
                            except Exception as notify_error:
                                logger.warning("Failed to send auto-ingest push failure alert: %s", notify_error)
            except Exception as cat_err:
                logger.warning("Error auto-ingesting category %s: %s", cat_key, cat_err)

        logger.info("Auto-ingest finished: scanned %d shows, newly pushed %d, skipped %d", total_scanned, len(pushed_shows), len(skipped_shows))
        return {
            "status": "success",
            "total_scanned": total_scanned,
            "pushed_count": len(pushed_shows),
            "pushed_shows": pushed_shows,
            "skipped_count": len(skipped_shows),
        }
