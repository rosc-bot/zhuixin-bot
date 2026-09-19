import re
import json
import time
import sqlite3
import asyncio
import logging
from datetime import datetime, date, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))
from typing import Any, Dict, List, Optional, Set, Tuple
import asyncpg
import aiohttp
from config import LOCAL_DB_PATH, TMDB_API_KEY, PG_DSN
from database import DatabaseService, _open_local_db

logger = logging.getLogger(__name__)

# In-memory caches
TMDB_DETAILS_CACHE: Dict[int, Dict[str, Any]] = {}
TMDB_SEASON_CACHE: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
RADAR_SNAPSHOT_CACHE: Dict[str, Tuple[Dict[str, Any], float]] = {}
RADAR_CACHE_TTL = 300.0  # 5 minutes


def _load_persisted_tmdb_cache_sync() -> Dict[int, Dict[str, Any]]:
    try:
        conn = _open_local_db()
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS tmdb_meta_cache (\n                tmdb_id INTEGER PRIMARY KEY,\n                meta_json TEXT NOT NULL,\n                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n            )\n        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS tmdb_season_cache (\n                tmdb_id INTEGER,\n                season INTEGER,\n                eps_json TEXT NOT NULL,\n                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                PRIMARY KEY(tmdb_id, season)\n            )\n        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS tmdb_title_match_cache (\n                clean_title TEXT NOT NULL,\n                season INTEGER NOT NULL DEFAULT 1,\n                tmdb_id INTEGER,\n                tmdb_title TEXT,\n                total_episodes INTEGER,\n                is_movie INTEGER DEFAULT 0,\n                PRIMARY KEY(clean_title, season)\n            )\n        """)
        conn.commit()
        rows = c.execute("SELECT tmdb_id, meta_json FROM tmdb_meta_cache").fetchall()
        conn.close()
        cache = {}
        for tid, data_str in rows:
            try:
                cache[tid] = json.loads(data_str)
            except Exception:
                pass
        return cache
    except Exception as e:
        logger.warning("Could not load persisted TMDB cache: %s", e)
        return {}


def _save_persisted_tmdb_cache_sync(tmdb_id: int, meta: Dict[str, Any]):
    try:
        conn = _open_local_db()
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO tmdb_meta_cache (tmdb_id, meta_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        """, (int(tmdb_id), json.dumps(meta, ensure_ascii=False)))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("Could not persist TMDB cache for %s: %s", tmdb_id, e)


def _load_persisted_season_cache_sync() -> Dict[Tuple[int, int], List[Dict[str, Any]]]:
    try:
        conn = _open_local_db()
        c = conn.cursor()
        rows = c.execute("SELECT tmdb_id, season, eps_json FROM tmdb_season_cache").fetchall()
        conn.close()
        cache = {}
        for tid, sea, data_str in rows:
            try:
                cache[(tid, sea)] = json.loads(data_str)
            except Exception:
                pass
        return cache
    except Exception as e:
        logger.warning("Could not load persisted season cache: %s", e)
        return {}


def _save_persisted_season_cache_sync(tmdb_id: int, season: int, eps: List[Dict[str, Any]]):
    try:
        conn = _open_local_db()
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO tmdb_season_cache (tmdb_id, season, eps_json, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """, (int(tmdb_id), int(season), json.dumps(eps, ensure_ascii=False)))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("Could not persist season cache for %s S%s: %s", tmdb_id, season, e)


# Initialize memory caches on startup
TMDB_DETAILS_CACHE.update(_load_persisted_tmdb_cache_sync())
TMDB_SEASON_CACHE.update(_load_persisted_season_cache_sync())


class LibraryService:
    @classmethod
    def invalidate_radar_cache(cls):
        """Clears radar snapshot cache to force instant recomputation."""
        RADAR_SNAPSHOT_CACHE.clear()

    @classmethod
    async def get_initial_share_url(cls, title: str, season: int = 1) -> Optional[str]:
        """Query PostgreSQL media_bot_db resources table for the very first accepted share URL
        of the specified title and season, with fallback to local SQLite auto_ingest_history.
        """
        clean_t = re.sub(r'[^\w一-龥]', '', str(title or ''))
        try:
            conn = await asyncpg.connect(PG_DSN)
            query = """
                SELECT r.share_url
                FROM resources r
                JOIN tasks t ON t.id = r.task_id
                WHERE (t.title = $1 OR t.title LIKE $2)
                  AND COALESCE(r.season, t.season, 1) = $3
                  AND r.status = 'ACCEPTED'
                  AND r.share_url IS NOT NULL
                ORDER BY r.created_at ASC
                LIMIT 1
            """
            row = await conn.fetchrow(query, title, f"%{clean_t}%", int(season or 1))
            await conn.close()
            if row and row["share_url"]:
                return str(row["share_url"]).strip()
        except Exception as e:
            logger.warning("Failed to fetch initial share URL from PG for %s S%02d: %s", title, season, e)

        try:
            from database import DatabaseService
            urls = await DatabaseService.get_historical_share_urls(title, season)
            if urls:
                return urls[-1]
        except Exception:
            pass
        return None

    @classmethod
    async def get_tmdb_series_meta(cls, tmdb_id: int) -> Optional[Dict[str, Any]]:
        if tmdb_id in TMDB_DETAILS_CACHE:
            return TMDB_DETAILS_CACHE[tmdb_id]

        if not TMDB_API_KEY:
            return None

        url = f"https://api.themoviedb.org/3/tv/{tmdb_id}"
        params = {
            "api_key": TMDB_API_KEY,
            "language": "zh-CN",
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        seasons_info = {}
                        for s in (data.get("seasons") or []):
                            s_num = s.get("season_number")
                            if s_num is not None and s_num > 0:
                                seasons_info[s_num] = {
                                    "season": s_num,
                                    "name": s.get("name"),
                                    "episodes": s.get("episode_count") or 0,
                                    "air_date": s.get("air_date"),
                                }
                        meta = {
                            "tmdb_id": tmdb_id,
                            "title": data.get("name") or data.get("original_name"),
                            "status": data.get("status"),
                            "number_of_seasons": data.get("number_of_seasons") or 1,
                            "number_of_episodes": data.get("number_of_episodes") or 0,
                            "seasons": seasons_info,
                        }
                        TMDB_DETAILS_CACHE[tmdb_id] = meta
                        await asyncio.to_thread(_save_persisted_tmdb_cache_sync, tmdb_id, meta)
                        return meta
        except Exception as e:
            logger.warning("Failed to fetch TMDB tv meta for %s: %s", tmdb_id, e)
        return None

    @classmethod
    async def get_tmdb_season_episodes(cls, tmdb_id: int, season: int) -> List[Dict[str, Any]]:
        cache_key = (tmdb_id, season)
        if cache_key in TMDB_SEASON_CACHE:
            return TMDB_SEASON_CACHE[cache_key]

        if not TMDB_API_KEY:
            return []

        url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}"
        params = {
            "api_key": TMDB_API_KEY,
            "language": "zh-CN",
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        raw_eps = data.get("episodes") or []
                        cleaned_eps = []
                        for ep in raw_eps:
                            cleaned_eps.append({
                                "episode_number": ep.get("episode_number"),
                                "name": ep.get("name"),
                                "air_date": ep.get("air_date"),
                                "overview": (ep.get("overview") or "")[:80],
                            })
                        TMDB_SEASON_CACHE[cache_key] = cleaned_eps
                        await asyncio.to_thread(_save_persisted_season_cache_sync, tmdb_id, season, cleaned_eps)
                        return cleaned_eps
        except Exception as e:
            logger.warning("Failed to fetch TMDB season episodes for %s S%s: %s", tmdb_id, season, e)
        return []

    @classmethod
    async def prefetch_all_tmdb_meta(cls, task_pairs: List[Tuple[int, int]]):
        """Prefetches series and season details concurrently."""
        uncached_series = [tid for tid, _ in set(task_pairs) if tid and tid not in TMDB_DETAILS_CACHE]
        uncached_seasons = [(tid, sea) for tid, sea in set(task_pairs) if tid and (tid, sea) not in TMDB_SEASON_CACHE]

        sem = asyncio.Semaphore(5)
        async def _fetch_series(tid: int):
            async with sem:
                await cls.get_tmdb_series_meta(tid)

        async def _fetch_season(pair: Tuple[int, int]):
            async with sem:
                await cls.get_tmdb_season_episodes(pair[0], pair[1])

        coros = [_fetch_series(tid) for tid in uncached_series] + [_fetch_season(pair) for pair in uncached_seasons]
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)

    @staticmethod
    def _absolute_episode_offset(groups: Dict[Tuple[str, int], Set[int]], clean_title: str, season: int) -> int:
        if season <= 1:
            return 0
        current = groups.get((clean_title, season)) or set()
        previous = groups.get((clean_title, season - 1)) or set()
        if not current or not previous:
            return 0
        current_min = min(current)
        previous_max = max(previous)
        if current_min >= 100 and current_min > previous_max and current_min - previous_max <= 5:
            return previous_max
        return 0

    @classmethod
    def _normalize_absolute_episode_entries(cls, entries: List[Dict[str, Any]]) -> None:
        groups: Dict[Tuple[str, int], Set[int]] = {}
        for entry in entries:
            key = (entry.get("clean_title", ""), int(entry.get("season") or 1))
            groups.setdefault(key, set()).update(int(e) for e in (entry.get("episodes") or []))
        offsets = {
            key: cls._absolute_episode_offset(groups, key[0], key[1])
            for key in groups
        }
        for entry in entries:
            key = (entry.get("clean_title", ""), int(entry.get("season") or 1))
            offset = offsets.get(key, 0)
            if offset:
                entry["episodes"] = sorted({int(e) - offset if int(e) > offset else int(e) for e in (entry.get("episodes") or [])})
                entry["start_episode"] = min(entry["episodes"]) if entry["episodes"] else 1
                entry["is_completed"] = len(entry["episodes"]) >= int(entry.get("total_episodes") or 0)

    @classmethod
    def _normalize_absolute_episode_map(
        cls, inventory: Dict[Tuple[str, int], Set[int]]
    ) -> Dict[Tuple[str, int], Set[int]]:
        offsets = {
            key: cls._absolute_episode_offset(inventory, key[0], key[1])
            for key in inventory
        }
        normalized: Dict[Tuple[str, int], Set[int]] = {}
        for key, episodes in inventory.items():
            offset = offsets.get(key, 0)
            normalized[key] = {
                int(ep) - offset if offset and int(ep) > offset else int(ep)
                for ep in episodes
            }
        return normalized

    @classmethod
    async def get_transferred_library(cls) -> List[Dict[str, Any]]:
        """
        Connects to PostgreSQL media_bot_db and extracts every transferred
        season-level record separately, enriched with real physical cloud disk inventory
        and automatic TMDB metadata resolution.
        """
        try:
            conn = await asyncpg.connect(PG_DSN)
            tv_rows = await conn.fetch("""
                SELECT 
                    t.id as task_id, 
                    t.title, 
                    t.media_type, 
                    COALESCE(r.season, t.season, 1) as season, 
                    t.start_episode,
                    t.total_episodes, 
                    t.tmdb_id, 
                    t.status, 
                    t.transfer_status,
                    array_agg(DISTINCT r.episode) as episodes
                FROM tasks t
                JOIN resources r ON r.task_id = t.id AND r.status = 'ACCEPTED' AND r.episode IS NOT NULL
                WHERE t.media_type != 'MOVIE'
                GROUP BY t.id, t.title, t.media_type, COALESCE(r.season, t.season, 1), t.start_episode, t.total_episodes, t.tmdb_id, t.status, t.transfer_status
                ORDER BY t.id, COALESCE(r.season, t.season, 1)
            """)

            movie_rows = await conn.fetch("""
                SELECT 
                    t.id as task_id, 
                    t.title, 
                    t.media_type, 
                    1 as season, 
                    t.start_episode,
                    t.total_episodes, 
                    t.tmdb_id, 
                    t.status, 
                    t.transfer_status,
                    ARRAY[1] as episodes
                FROM tasks t
                WHERE t.media_type = 'MOVIE' AND (t.transfer_status = 'SUCCESS' OR t.status = 'COMPLETED')
                ORDER BY t.id DESC
            """)
            await conn.close()
        except Exception as e:
            logger.warning("Failed to connect to PostgreSQL media_bot_db: %s", e)
            return []

        entries: List[Dict[str, Any]] = []
        for r in tv_rows:
            raw_title = str(r["title"] or "").strip()
            clean_t = re.sub(r'[^\w\u4e00-\u9fa5]', '', raw_title)
            eps = sorted([e for e in (r["episodes"] or []) if e is not None])
            season = r["season"] if r["season"] is not None else 1
            media_type = r["media_type"] or "TV"
            total_eps = r["total_episodes"] or (len(eps) if eps else 1)
            start_ep = r["start_episode"] or 1

            entries.append({
                "id": r["task_id"],
                "task_id": r["task_id"],
                "title": raw_title,
                "clean_title": clean_t,
                "media_type": media_type,
                "season": season,
                "start_episode": start_ep,
                "total_episodes": total_eps,
                "tmdb_id": r["tmdb_id"],
                "episodes": eps,
                "transfer_status": r["transfer_status"],
                "is_completed": (len(eps) >= total_eps),
            })

        for r in movie_rows:
            raw_title = str(r["title"] or "").strip()
            clean_t = re.sub(r'[^\w\u4e00-\u9fa5]', '', raw_title)
            entries.append({
                "id": r["task_id"],
                "task_id": r["task_id"],
                "title": raw_title,
                "clean_title": clean_t,
                "media_type": "MOVIE",
                "season": 1,
                "start_episode": 1,
                "total_episodes": 1,
                "tmdb_id": r["tmdb_id"],
                "episodes": [1],
                "transfer_status": r["transfer_status"],
                "is_completed": True,
            })

        # Load TMDB title match cache
        def _get_match_cache():
            try:
                conn_c = _open_local_db()
                rows = conn_c.cursor().execute("SELECT clean_title, season, tmdb_id, total_episodes, is_movie FROM tmdb_title_match_cache").fetchall()
                conn_c.close()
                return {(r[0], r[1]): (r[2], r[3], r[4]) for r in rows}
            except Exception:
                return {}

        match_cache = await asyncio.to_thread(_get_match_cache)

        # Apply match cache to existing entries with missing/invalid tmdb_id
        for entry in entries:
            ct = entry["clean_title"]
            sea = int(entry.get("season") or 1)
            if (ct, sea) in match_cache:
                m_tid, m_tot, m_is_mov = match_cache[(ct, sea)]
                if not entry.get("tmdb_id") and m_tid:
                    entry["tmdb_id"] = m_tid
                if m_tot and m_tot > (entry.get("total_episodes") or 0):
                    entry["total_episodes"] = m_tot
                if m_is_mov:
                    entry["media_type"] = "MOVIE"

        # Replace task-level cumulative totals with authoritative per-season totals
        unique_tmdb_ids = {e.get("tmdb_id") for e in entries if e.get("tmdb_id")}
        if unique_tmdb_ids:
            await asyncio.gather(
                *(cls.get_tmdb_series_meta(int(tid)) for tid in unique_tmdb_ids),
                return_exceptions=True,
            )
            for entry in entries:
                tid = entry.get("tmdb_id")
                if not tid:
                    continue
                meta = TMDB_DETAILS_CACHE.get(tid)
                if entry.get("media_type") == "MOVIE":
                    continue
                season = int(entry.get("season") or 1)
                season_meta = (meta or {}).get("seasons", {}).get(season)
                if season_meta is None:
                    season_meta = (meta or {}).get("seasons", {}).get(str(season))
                if season_meta and season_meta.get("episodes"):
                    entry["total_episodes"] = int(season_meta["episodes"])

        # Canonicalize season-relative episode keys before physical merge.
        cls._normalize_absolute_episode_entries(entries)

        # === Merge Real Physical Cloud Disk Inventory ===
        try:
            from services.cloud_inventory_service import CloudInventoryService
            phys_inv = await CloudInventoryService.get_physical_inventory()
            phys_inv = cls._normalize_absolute_episode_map(phys_inv)
            
            entries_map = {}
            for e in entries:
                entries_map[(e["clean_title"], e["season"])] = e

            for (p_clean_title, p_season), p_eps in phys_inv.items():
                key = (p_clean_title, p_season)
                if key in entries_map:
                    existing = entries_map[key]
                    merged_eps = sorted(list(set(existing["episodes"]) | set(p_eps)))
                    existing["episodes"] = merged_eps
                    # Do NOT blindly mark completed! Compare against total_episodes
                    tot = existing.get("total_episodes") or 0
                    if tot > 0 and len(merged_eps) >= tot:
                        existing["is_completed"] = True
                    else:
                        existing["is_completed"] = False
                else:
                    # Cloud discovered item: check match cache
                    cache_info = match_cache.get((p_clean_title, p_season))
                    c_tid = cache_info[0] if cache_info else None
                    c_tot = cache_info[1] if cache_info else None
                    c_is_movie = bool(cache_info[2]) if cache_info else (len(p_eps) == 1)

                    actual_tot = c_tot if c_tot else (max(p_eps) if p_eps else len(p_eps))
                    is_done = (c_is_movie or (actual_tot > 0 and len(p_eps) >= actual_tot))

                    entries.append({
                        "id": 990000 + len(entries),
                        "task_id": None,
                        "title": p_clean_title,
                        "clean_title": p_clean_title,
                        "media_type": "MOVIE" if c_is_movie else "TV",
                        "season": p_season,
                        "start_episode": min(p_eps) if p_eps else 1,
                        "total_episodes": actual_tot,
                        "tmdb_id": c_tid,
                        "episodes": sorted(list(p_eps)),
                        "transfer_status": "COMPLETED",
                        "is_completed": is_done,
                        "is_cloud_discovered": True,
                    })
        except Exception as err:
            logger.warning("Error merging physical cloud inventory: %s", err)

        return entries

    @classmethod
    def match_show_in_library(
        cls,
        library_entries: List[Dict[str, Any]],
        title: str,
        season: int = 1
    ) -> Optional[Dict[str, Any]]:
        clean_target = re.sub(r'[^\w\u4e00-\u9fa5]', '', title)
        for entry in library_entries:
            if entry["clean_title"] == clean_target:
                if entry.get("media_type") == "MOVIE" or entry.get("season") == season:
                    return entry

        for entry in library_entries:
            if entry["title"] == title:
                if entry.get("media_type") == "MOVIE" or entry.get("season") == season:
                    return entry

        for entry in library_entries:
            if len(clean_target) >= 2 and len(entry["clean_title"]) >= 2:
                if clean_target in entry["clean_title"] or entry["clean_title"] in clean_target:
                    if entry.get("media_type") == "MOVIE" or entry.get("season") == season:
                        return entry
        return None

    @classmethod
    async def evaluate_item_status(
        cls,
        lib_entry: Optional[Dict[str, Any]],
        target_episodes: List[int],
        is_following: bool = False,
        follow_mode: str = "LATEST"
    ) -> Dict[str, Any]:
        if not lib_entry:
            if is_following:
                return {
                    "in_library": False,
                    "badge": "⭐ [追更中 · 待入库]",
                    "missing_episodes": target_episodes,
                    "collected_episodes": [],
                    "action_label": "🔄 立即打捞",
                    "action_type": "scout",
                }
            return {
                "in_library": False,
                "badge": "🆕 [未收录]",
                "missing_episodes": target_episodes,
                "collected_episodes": [],
                "action_label": "➕ 加入追更",
                "action_type": "follow",
            }

        collected = set(lib_entry.get("episodes") or [])
        target_set = set(target_episodes or [])
        cur_season = lib_entry.get("season") or 1
        tmdb_id = lib_entry.get("tmdb_id")

        if lib_entry.get("media_type") == "MOVIE":
            return {
                "in_library": True,
                "badge": "🟢 [全片已入库]",
                "missing_episodes": [],
                "collected_episodes": [1],
                "action_label": "✅ 已入库全片",
                "action_type": "completed",
            }

        official_ep_count = None
        if tmdb_id:
            tmdb_meta = await cls.get_tmdb_series_meta(tmdb_id)
            if tmdb_meta:
                s_data = tmdb_meta.get("seasons", {}).get(cur_season) or tmdb_meta.get("seasons", {}).get(str(cur_season))
                if s_data:
                    official_ep_count = s_data.get("episodes")

        if not official_ep_count:
            official_ep_count = lib_entry.get("total_episodes") or len(collected)

        is_season_complete = False
        tot_eps = lib_entry.get("total_episodes")
        if tot_eps and len(collected) >= tot_eps and set(range(1, tot_eps + 1)).issubset(collected):
            is_season_complete = True
            official_ep_count = tot_eps
        elif official_ep_count and len(collected) >= official_ep_count:
            if set(range(1, official_ep_count + 1)).issubset(collected):
                is_season_complete = True

        if is_season_complete:
            return {
                "in_library": True,
                "badge": f"✅ [S{cur_season}已收齐 ({len(collected)}/{official_ep_count})]",
                "missing_episodes": [],
                "collected_episodes": sorted(list(collected)),
                "action_label": "✅ 全季已入库",
                "action_type": "completed",
            }

        if target_set:
            missing_today = [e for e in sorted(target_set) if e not in collected]
            if not missing_today:
                max_ep = max(collected) if collected else 0
                return {
                    "in_library": True,
                    "badge": f"🟢 [已入库至E{max_ep:02d}]",
                    "missing_episodes": [],
                    "collected_episodes": sorted(list(collected)),
                    "action_label": "✅ 今日已收录",
                    "action_type": "completed",
                }
            else:
                missing_str = ",".join(f"E{e:02d}" for e in missing_today)
                return {
                    "in_library": True,
                    "badge": f"🔴 [缺集待补 · 缺{missing_str}]",
                    "missing_episodes": missing_today,
                    "collected_episodes": sorted(list(collected)),
                    "action_label": f"🔥 打捞缺集 ({missing_str})",
                    "action_type": "scout",
                }
        else:
            max_coll = max(collected) if collected else 0
            all_missing = [e for e in range(1, official_ep_count + 1) if e not in collected]

            if follow_mode == "LATEST":
                eff_missing = [e for e in all_missing if e > max_coll]
                diag_msg = f"⚡ 仅追最新: 待补 S{cur_season} 新播集"
            else:
                eff_missing = all_missing
                diag_msg = f"🔄 全量补齐: 前期缺收 {len([e for e in all_missing if e <= max_coll])} 集 · 待补新集"

            miss_str = f"缺{len(eff_missing)}集" if eff_missing else "待播更新"
            return {
                "in_library": True,
                "badge": f"🟡 [S{cur_season}连载至E{max_coll:02d} · {miss_str}]",
                "missing_episodes": eff_missing,
                "collected_episodes": sorted(list(collected)),
                "action_label": "🔄 检查打捞缺集",
                "action_type": "scout",
                "diag_text": diag_msg,
            }

    @classmethod
    async def get_radar_summary(
        cls,
        today_shows: List[Dict[str, Any]],
        follow_mode: str = "LATEST",
        force_refresh: bool = False
    ) -> Dict[str, Any]:
        """
        Produces rigorous season-level radar summary.
        Core mission: FOCUS ON ONGOING UPDATES (追新), catching newly aired episodes!
        In 'LATEST' mode, priority is strictly given to trailing new episodes (e > max_coll).
        """
        now = time.time()
        if not force_refresh and follow_mode in RADAR_SNAPSHOT_CACHE:
            cached_data, cached_at = RADAR_SNAPSHOT_CACHE[follow_mode]
            if now - cached_at < RADAR_CACHE_TTL:
                return cached_data

        library_entries = await cls.get_transferred_library()
        ignored_rules = await DatabaseService.get_ignored_rules()
        today_str = datetime.now(BEIJING_TZ).date().isoformat()

        # Prefetch metadata for all unique (tmdb_id, season) pairs
        task_pairs = [(e["tmdb_id"], e["season"]) for e in library_entries if e.get("tmdb_id")]
        await cls.prefetch_all_tmdb_meta(task_pairs)

        airing_today_matches = []
        for s in today_shows:
            t = s.get("title")
            eps = s.get("episodes") or []
            if not eps:
                continue
            sea = s.get("season", 1)
            match = cls.match_show_in_library(library_entries, t, sea)
            if match:
                rules = ignored_rules.get((t, sea), set()) | ignored_rules.get((match.get("clean_title"), sea), set())
                is_all_ignored = (0 in rules)

                eval_res = await cls.evaluate_item_status(match, eps, follow_mode=follow_mode)
                if is_all_ignored:
                    eval_res["badge"] = "🚫 [缺集提醒已关闭]"
                    eval_res["action_label"] = "🚫 提醒已关闭"
                    eval_res["action_type"] = "ignored"
                elif rules:
                    eff_m = [e for e in eval_res.get("missing_episodes", []) if e not in rules]
                    eval_res["missing_episodes"] = eff_m
                    if not eff_m and eval_res["action_type"] == "scout":
                        eval_res["badge"] = "🟢 [缺集已忽略]"
                        eval_res["action_label"] = "✅ 已忽略缺集"
                        eval_res["action_type"] = "completed"

                airing_today_matches.append({
                    "title": t,
                    "season": sea,
                    "today_episodes": eps,
                    "ep_display": s.get("ep_display"),
                    "library_entry": match,
                    "eval": eval_res,
                })

        missing_in_library = []
        completed_in_library = []

        for entry in library_entries:
            if entry["media_type"] == "MOVIE":
                completed_in_library.append(entry)
                continue

            cur_season = entry["season"] or 1
            collected = entry["episodes"]
            tmdb_id = entry["tmdb_id"]
            raw_title = entry["title"]
            clean_t = entry["clean_title"]

            rules = ignored_rules.get((raw_title, cur_season), set()) | ignored_rules.get((clean_t, cur_season), set())
            is_all_ignored = (0 in rules)

            tmdb_meta = TMDB_DETAILS_CACHE.get(tmdb_id) if tmdb_id else None

            # Fetch detailed episode list with air dates
            season_eps = TMDB_SEASON_CACHE.get((tmdb_id, cur_season)) or [] if tmdb_id else []

            # AIR DATE GATE: Only count episodes with air_date <= today as AIRED
            aired_numbers = []
            future_numbers = []
            for ep in season_eps:
                ep_num = ep.get("episode_number")
                if not ep_num:
                    continue
                air_d = ep.get("air_date")
                if air_d and air_d <= today_str:
                    aired_numbers.append(ep_num)
                elif air_d and air_d > today_str:
                    future_numbers.append(ep_num)
                else:
                    # air_date is None: if series is ended, treat as aired; otherwise if ep_num <= max_coll treat as aired
                    if (tmdb_meta or {}).get("status") in ("Ended", "Canceled"):
                        aired_numbers.append(ep_num)
                    elif ep_num <= (max(collected) if collected else 0):
                        aired_numbers.append(ep_num)
                    else:
                        future_numbers.append(ep_num)

            # Fallback if TMDB season episodes not populated
            if not aired_numbers:
                s_data = tmdb_meta.get("seasons", {}).get(cur_season) or tmdb_meta.get("seasons", {}).get(str(cur_season)) if tmdb_meta else None
                ep_cnt = s_data.get("episodes") if s_data else (entry["total_episodes"] or len(collected))
                aired_numbers = list(range(1, ep_cnt + 1))

            max_coll = max(collected) if collected else 0
            min_coll = min(collected) if collected else 1
            latest_aired = max(aired_numbers) if aired_numbers else (max_coll or 1)

            # REAL missing in THIS season
            real_missing_aired = [e for e in aired_numbers if e not in collected]

            # In LATEST mode:
            # 1. Trailing new updates that have AIRED (e > max_coll) are PRIMARY ZhuiXin targets!
            # 2. Mid gaps (min_coll < e < max_coll) are internal gaps.
            # 3. Early gaps (e < min_coll) are only shown in FULL mode, or if max_coll == 0.
            trailing_new = [e for e in real_missing_aired if e > max_coll]
            mid_gaps = [e for e in real_missing_aired if min_coll < e < max_coll]
            early_gaps = [e for e in real_missing_aired if e < min_coll]

            if follow_mode == "LATEST":
                if max_coll > 0:
                    # ZhuiXin priority: Only track trailing new episodes and internal missing gaps!
                    effective_missing = trailing_new + mid_gaps
                else:
                    effective_missing = real_missing_aired
            else:
                effective_missing = real_missing_aired

            tot_eps = entry.get("total_episodes")
            has_internal_gap = bool(mid_gaps)

            tmdb_st = str((tmdb_meta or {}).get("status") or "").lower()
            is_tmdb_ended = tmdb_st in ("ended", "canceled")
            is_ongoing = (not is_tmdb_ended) or bool(future_numbers) or (bool(tot_eps) and len(collected) < tot_eps)
            entry["is_ongoing"] = is_ongoing

            is_season_aired_complete = (len(effective_missing) == 0 and len(collected) >= len(aired_numbers)) or (
                tot_eps and len(collected) >= tot_eps and not has_internal_gap and min_coll == 1 and not is_ongoing
            )

            if is_season_aired_complete:
                eff_target = tot_eps if (tot_eps and len(collected) >= tot_eps) else (len(aired_numbers) if aired_numbers else (len(collected) or 1))
                if is_ongoing:
                    reason = f"S{cur_season} 连载已跟至最新 (已收 {len(collected)}/{eff_target} 集)"
                    if future_numbers:
                        reason += f" · ⏳ 待播: E{min(future_numbers):02d}"
                else:
                    reason = f"S{cur_season} 已全集收齐 ({len(collected)}/{eff_target})"
                completed_in_library.append({
                    **entry,
                    "effective_target": eff_target,
                    "latest_aired": latest_aired,
                    "reason_text": reason,
                    "is_ongoing": is_ongoing,
                })
            else:
                if is_all_ignored:
                    entry_copy = dict(entry)
                    entry_copy["reason_text"] = f"S{cur_season} 缺集提醒已主动关闭"
                    entry_copy["missing_episodes"] = []
                    entry_copy["is_ignored"] = True
                    entry_copy["ignored_episodes"] = [0]
                    entry_copy["is_ongoing"] = is_ongoing
                    completed_in_library.append(entry_copy)
                    continue

                active_missing = [e for e in effective_missing if e not in rules]
                ignored_this = [e for e in effective_missing if e in rules]

                if not active_missing:
                    entry_copy = dict(entry)
                    ign_str = ",".join(f"E{e:02d}" for e in sorted(ignored_this)) if ignored_this else "已忽略"
                    entry_copy["reason_text"] = f"S{cur_season} 缺集已主动设置忽略 ({ign_str}) · 监控新集"
                    entry_copy["missing_episodes"] = []
                    entry_copy["is_ignored"] = True
                    entry_copy["ignored_episodes"] = ignored_this
                    entry_copy["is_ongoing"] = is_ongoing
                    completed_in_library.append(entry_copy)
                else:
                    diag_reasons = []
                    if trailing_new:
                        trail_str = ", ".join(f"E{e:02d}" for e in trailing_new[:3])
                        if len(trailing_new) > 3:
                            trail_str += f" 等共{len(trailing_new)}集"
                        diag_reasons.append(f"🔥 追新待收: {trail_str}")
                    if mid_gaps:
                        mid_str = ", ".join(f"E{e:02d}" for e in mid_gaps)
                        diag_reasons.append(f"⚠️ 断集缺漏: {mid_str}")
                    if early_gaps and (follow_mode == "FULL" or max_coll == 0):
                        if len(early_gaps) == 1:
                            diag_reasons.append(f"前期缺收: E{early_gaps[0]:02d}")
                        else:
                            diag_reasons.append(f"前期缺收: E{min(early_gaps):02d}-E{max(early_gaps):02d}")
                    if future_numbers:
                        diag_reasons.append(f"⏳ 待播: 从E{min(future_numbers):02d}起未开播")

                    diag = " · ".join(diag_reasons) if diag_reasons else "待更新"
                    ign_joined = ",".join(f"E{e:02d}" for e in sorted(ignored_this))
                    ign_note = f" (已忽略{ign_joined})" if ignored_this else ""
                    missing_in_library.append({
                        **entry,
                        "effective_target": len(aired_numbers),
                        "latest_aired": latest_aired,
                        "missing_episodes": active_missing,
                        "all_missing_episodes": real_missing_aired,
                        "future_episodes": future_numbers,
                        "ignored_episodes": ignored_this,
                        "reason_text": f"{diag}{ign_note}",
                        "follow_mode": follow_mode,
                        "is_ongoing": is_ongoing,
                    })

        # Auto sync all ongoing shows to watchlist (await 保证不丢任务)
        async def _do_sync(item):
            return await DatabaseService.sync_ongoing_to_watchlist(
                title=item["title"],
                season=item.get("season", 1),
                tmdb_id=item.get("tmdb_id"),
                total_episodes=item.get("total_episodes") or len(item.get("episodes", [])),
                last_aired=item.get("latest_aired") or max(item.get("episodes", []) or [1]),
                collected_eps=item.get("episodes", []),
                is_ongoing=item.get("is_ongoing", True),
            )

        sync_targets = [
            item for item in (missing_in_library + completed_in_library)
            if item.get("media_type") != "MOVIE" and item.get("is_ongoing")
        ]
        if sync_targets:
            sync_results = await asyncio.gather(
                *[_do_sync(item) for item in sync_targets],
                return_exceptions=True,
            )
            sync_errors = [r for r in sync_results if isinstance(r, BaseException)]
            if sync_errors:
                logger.error(
                    "Failed to auto-sync %d/%d ongoing shows to watchlist; first error: %s",
                    len(sync_errors), len(sync_targets), sync_errors[0],
                )

        result = {
            "total_library_tasks": len(set(e["id"] for e in library_entries)),
            "airing_today_matches": airing_today_matches,
            "missing_in_library": missing_in_library,
            "completed_in_library": completed_in_library,
        }
        RADAR_SNAPSHOT_CACHE[follow_mode] = (result, now)
        return result
