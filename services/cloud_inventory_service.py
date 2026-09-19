import json
"""Real Cloud Inventory Service for scanning physical files in '影视转存总目录' on Guangya Cloud."""

import re
import time
import socket
import sqlite3
import asyncio
import logging
from typing import Any, Dict, List, Optional, Set, Tuple
import aiohttp
from config import LOCAL_DB_PATH, PG_DSN
from database import _open_local_db

logger = logging.getLogger(__name__)

VIDEO_EXTS = (".mkv", ".mp4", ".ts", ".avi", ".mov", ".wmv", ".flv", ".webm", ".iso", ".strm")


def _init_inventory_db_sync():
    conn = _open_local_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS cloud_disk_inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            clean_title TEXT NOT NULL,
            season INTEGER NOT NULL DEFAULT 1,
            tmdb_id INTEGER,
            episode INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            rel_path TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(clean_title, tmdb_id, season, episode)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS cloud_scan_meta (
            key TEXT PRIMARY KEY,
            val TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


class CloudInventoryService:
    @classmethod
    async def init_db(cls):
        await asyncio.to_thread(_init_inventory_db_sync)

    @staticmethod
    def parse_season_episode_from_filename(filename: str, default_season: int = 1) -> Tuple[int, Optional[int]]:
        fn = filename.strip()
        # S2026E39-style year/week labels are not season/episode numbers.
        if re.search(r"(?<![A-Za-z0-9])S\d{4}\s*E\d{1,4}(?!\d)", fn, re.I):
            return default_season, None
        season = default_season
        m_se = re.search(r"S(\d{1,2})\s*E(\d{1,4})", fn, re.I)
        ep = None
        if m_se:
            season = int(m_se.group(1))
            ep = int(m_se.group(2))
        else:
            m_cn_s = re.search(r"第\s*([一二三四五六七八九十0-9]+)\s*季", fn)
            if m_cn_s:
                s_raw = m_cn_s.group(1)
                cn_nums = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
                season = int(s_raw) if s_raw.isdigit() else cn_nums.get(s_raw, default_season)

            m_ep = re.search(r"(?:[Ee]|EP|ep)\s*0*(\d{1,4})\b", fn)
            if m_ep:
                ep = int(m_ep.group(1))
            else:
                m_cn_ep = re.search(r"第\s*0*(\d{1,4})\s*(?:集|话)", fn)
                if m_cn_ep:
                    ep = int(m_cn_ep.group(1))
                else:
                    m_simple_ep = re.search(r"(?:[._\-\s])(\d{2,4})(?:[._\-\s]|\.[a-zA-Z0-9]+$)", fn)
                    if m_simple_ep:
                        val = int(m_simple_ep.group(1))
                        if 1 <= val <= 2500 and not (1900 <= val <= 2099) and val not in (1080, 2160, 720, 480, 264, 265):
                            ep = val

        return season, ep

    @staticmethod
    def normalize_absolute_episode_items(items: List[Tuple[Any, ...]]) -> List[Tuple[Any, ...]]:
        """Normalize high absolute episode numbers only with a prior-season fence."""
        groups: Dict[Tuple[str, int], List[int]] = {}
        for item in items:
            if len(item) < 5 or item[1] is None or item[3] is None or item[4] is None:
                continue
            groups.setdefault((str(item[1]), int(item[3])), []).append(int(item[4]))

        offsets: Dict[Tuple[str, int], int] = {}
        for (clean_title, season), current_eps in groups.items():
            if season <= 1:
                continue
            previous_eps = groups.get((clean_title, season - 1)) or []
            if not previous_eps or not current_eps:
                continue
            previous_max = max(previous_eps)
            current_min = min(current_eps)
            if (
                current_min >= 100
                and current_min > previous_max
                and current_min - previous_max <= 5
            ):
                offsets[(clean_title, season)] = previous_max

        normalized: List[Tuple[Any, ...]] = []
        for item in items:
            if len(item) < 5:
                normalized.append(item)
                continue
            key = (str(item[1]), int(item[3]))
            offset = offsets.get(key, 0)
            if offset and int(item[4]) > offset:
                mutable = list(item)
                mutable[4] = int(item[4]) - offset
                normalized.append(tuple(mutable))
            else:
                normalized.append(item)
        return normalized

    @classmethod
    async def scan_guangya_master(cls) -> Dict[str, Any]:
        """
        Directly scans Guangya '影视转存总目录' (1942305989699285071)
        and persists real physical cloud files into local database.
        """
        t0 = time.time()
        import asyncpg

        # 1. Read Guangya auth token from database
        try:
            conn = await asyncpg.connect(PG_DSN)
            row = await conn.fetchrow("SELECT auth_token, target_folder_id FROM cloud_configs WHERE name = 'guangya'")
            await conn.close()
        except Exception as e:
            logger.warning("Could not connect to PG for cloud_configs: %s", e)
            return {"success": False, "error": f"数据库连接失败: {e}"}

        if not row or not row["auth_token"]:
            return {"success": False, "error": "未配置光鸭网盘凭证"}

        auth_data = json.loads(row["auth_token"])
        ref_token = auth_data.get("refresh_token")
        master_id = row["target_folder_id"] or "1942305989699285071"

        # 2. Get fresh access token via account.guangyapan.com
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            try:
                t_resp = await session.post(
                    "https://account.guangyapan.com/v1/auth/token",
                    json={"client_id": "aMe-8VSlkrbQXpUR", "grant_type": "refresh_token", "refresh_token": ref_token},
                    headers={"Content-Type": "application/json", "Origin": "https://account.guangyapan.com", "User-Agent": "Mozilla/5.0"},
                    timeout=aiohttp.ClientTimeout(total=15)
                )
                t_data = await t_resp.json()
                acc_token = t_data.get("access_token")
                if not acc_token:
                    return {"success": False, "error": f"光鸭Token刷新失败: {t_data}"}
            except Exception as e:
                return {"success": False, "error": f"连接光鸭认证服务器失败: {e}"}

            headers = {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {acc_token}",
                "Origin": "https://www.guangyapan.com",
                "User-Agent": "Mozilla/5.0",
            }

            sem = asyncio.Semaphore(3)
            async def list_dir(parent_id: str) -> List[Dict[str, Any]]:
                async with sem:
                    last_err = None
                    for attempt in range(5):
                        try:
                            r = await session.post(
                                "https://api.guangyapan.com/userres/v1/file/get_file_list",
                                json={"parentId": str(parent_id or ""), "pageNum": 1, "pageSize": 300},
                                headers=headers,
                                timeout=aiohttp.ClientTimeout(total=15)
                            )
                            if r.status == 401:
                                logger.error("Guangya API 401 Unauthorized on list_dir %s (token expired)", parent_id)
                                return []
                            if r.status == 429:
                                logger.warning("Guangya API 429 rate-limit on list_dir %s; attempt %d", parent_id, attempt+1)
                                await asyncio.sleep(2 ** attempt * 0.5)
                                last_err = "rate-limited"
                                continue
                            if r.status != 200:
                                logger.warning("Guangya API %d on list_dir %s; attempt %d", r.status, parent_id, attempt+1)
                                last_err = f"HTTP {r.status}"
                                await asyncio.sleep(2 ** attempt * 0.5)
                                continue
                            res = await r.json()
                            data = res.get("data", {})
                            items = data.get("list", [])
                            total = data.get("total")
                            if total and total > len(items) and total <= 5000:
                                try:
                                    r_all = await session.post(
                                        "https://api.guangyapan.com/userres/v1/file/get_file_list",
                                        json={"parentId": str(parent_id or ""), "pageNum": 1, "pageSize": total},
                                        headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=25)
                                    )
                                    if r_all.status == 200:
                                        res_all = await r_all.json()
                                        items = res_all.get("data", {}).get("list", []) or items
                                except Exception as e_all:
                                    logger.warning("Guangya large dir re-fetch failed for %s: %s", parent_id, e_all)
                            return items
                        except Exception as e:
                            last_err = str(e)
                            logger.warning("Guangya list_dir %s attempt %d failed: %s", parent_id, attempt+1, e)
                            await asyncio.sleep(2 ** attempt * 0.5)
                logger.error("Guangya list_dir %s exhausted 3 attempts, last error: %s", parent_id, last_err)
                return []

            # 3. Traverse Level 1 (电视剧, 电影)
            l1 = await list_dir(master_id)
            l2_tasks = [list_dir(it1.get("fileId")) for it1 in l1]
            l2_results = await asyncio.gather(*l2_tasks, return_exceptions=True)
            l2_results = [r if isinstance(r, list) else [] for r in l2_results]
            all_l2 = [item for sublist in l2_results for item in sublist]

            # 4. Traverse Level 3 (Show folders)
            l3_tasks = [list_dir(it2.get("fileId")) for it2 in all_l2]
            l3_results = await asyncio.gather(*l3_tasks, return_exceptions=True)
            l3_results = [r if isinstance(r, list) else [] for r in l3_results]
            shows = []
            for i, it2 in enumerate(all_l2):
                cat_name = it2.get("fileName")
                for it3 in l3_results[i]:
                    shows.append((cat_name, it3.get("fileName"), it3.get("fileId"), it3.get("resType")))

            # 5. List inner files and season subfolders
            inner_tasks = [list_dir(fid) for _, _, fid, rtype in shows if rtype == 2]
            inner_results = await asyncio.gather(*inner_tasks, return_exceptions=True)
            inner_results = [r if isinstance(r, list) else [] for r in inner_results]

            season_tasks = []
            season_meta = []
            show_items = [] # (title, clean_title, tmdb_id, season, ep, file_name, rel_path)

            for idx, (cat_name, folder_name, show_fid, rtype) in enumerate(shows):
                if rtype != 2:
                    continue
                # Parse TMDB ID and clean title from folder name
                # e.g. 人生赢家(2026)第一季 4K {tmdbid-331218}
                m_tid = re.search(r'tmdbid-(\d+)', folder_name)
                tmdb_id = int(m_tid.group(1)) if m_tid else None

                cn_s_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
                m_s_folder = re.search(r'第\s*([一二三四五六七八九十0-9]+)\s*季', folder_name)
                folder_def_s = 1
                if m_s_folder:
                    val = m_s_folder.group(1)
                    folder_def_s = int(val) if val.isdigit() else cn_s_map.get(val, 1)
                else:
                    m_s_en = re.search(r'Season\s*0*(\d+)', folder_name, re.I)
                    if m_s_en:
                        folder_def_s = int(m_s_en.group(1))

                # Clean title: strip bracketed metadata, year, 4K, and explicit season tags
                m_clean = re.sub(r'\(.*?\)|\[.*?\]|\{.*?\}', '', folder_name)
                m_clean = re.sub(r'第\s*[一二三四五六七八九十0-9]+\s*季', '', m_clean)
                m_clean = re.sub(r'Season\s*\d+', '', m_clean, flags=re.I)
                m_clean = re.sub(r'\s*4K.*', '', m_clean).strip()
                clean_title = re.sub(r'[^\w\u4e00-\u9fa5]', '', m_clean)

                inner_files = inner_results[idx]
                for item in inner_files:
                    fn = item.get("fileName", "")
                    if item.get("resType") == 2 and not fn.startswith("."): # Season folder e.g. 第一季
                        season_tasks.append(list_dir(item.get("fileId")))
                        season_meta.append((m_clean, clean_title, tmdb_id, fn))
                    elif any(fn.lower().endswith(ext) for ext in VIDEO_EXTS):
                        s, ep = cls.parse_season_episode_from_filename(fn, default_season=folder_def_s)
                        if ep is not None:
                            show_items.append((m_clean, clean_title, tmdb_id, s, ep, fn, fn))
                        else:
                            # Movie or full video
                            show_items.append((m_clean, clean_title, tmdb_id, folder_def_s, 1, fn, fn))

            # 6. Gather season files
            if season_tasks:
                season_results = await asyncio.gather(*season_tasks, return_exceptions=True)
                season_results = [r if isinstance(r, list) else [] for r in season_results]
                for (title, clean_t, tmdb_id, s_folder_name), s_files in zip(season_meta, season_results):
                    # parse season number from season folder name e.g. 第二季 -> 2
                    cn_s_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
                    m_s = re.search(r'第\s*([一二三四五六七八九十0-9]+)\s*季', s_folder_name)
                    def_s = 1
                    if m_s:
                        val = m_s.group(1)
                        def_s = int(val) if val.isdigit() else cn_s_map.get(val, 1)
                    else:
                        m_s_en = re.search(r'Season\s*0*(\d+)', s_folder_name, re.I)
                        if m_s_en:
                            def_s = int(m_s_en.group(1))

                    for item in s_files:
                        fn = item.get("fileName", "")
                        if any(fn.lower().endswith(ext) for ext in VIDEO_EXTS):
                            s, ep = cls.parse_season_episode_from_filename(fn, default_season=def_s)
                            if ep is not None:
                                show_items.append((title, clean_t, tmdb_id, s, ep, fn, f"{s_folder_name}/{fn}"))

        # 7. Persist into SQLite
        def _save_to_sqlite(items):
            conn = _open_local_db()
            c = conn.cursor()
            c.execute("DELETE FROM cloud_disk_inventory")
            c.executemany("""
                INSERT OR REPLACE INTO cloud_disk_inventory (title, clean_title, tmdb_id, season, episode, file_name, rel_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, items)
            c.execute("INSERT OR REPLACE INTO cloud_scan_meta (key, val) VALUES ('last_scan_at', CURRENT_TIMESTAMP)")
            c.execute("INSERT OR REPLACE INTO cloud_scan_meta (key, val) VALUES ('total_files', ?)", (len(items),))
            conn.commit()
            conn.close()

        # Normalize absolute numbering before persisting, so physical inventory
        # merges with PostgreSQL using the same canonical (season, episode) key.
        show_items = cls.normalize_absolute_episode_items(show_items)
        await asyncio.to_thread(_save_to_sqlite, show_items)

        t1 = time.time()
        logger.info("Guangya physical cloud scan finished in %.2f s. Persisted %d files across %d titles.", t1 - t0, len(show_items), len(shows))
        rec_res = await cls.reconcile_physical_inventory()
        return {
            "success": True,
            "scan_duration": round(t1 - t0, 2),
            "total_files": len(show_items),
            "total_titles": len(shows),
            "purged_count": rec_res.get("purged_count", 0),
            "purged_items": rec_res.get("purged_items", []),
        }

    @classmethod
    async def reconcile_physical_inventory(cls, max_age_minutes: int = 30) -> Dict[str, Any]:
        """
        Reconcile Postgres media_bot_db resources against physical inventory on Guangya Cloud.
        If a TV series folder exists in physical inventory on Guangya Cloud, but specific
        episodes recorded as 'ACCEPTED' in Postgres are missing from physical files (and were
        created at least max_age_minutes ago and not in an active in-flight transfer queue),
        update their status to 'DELETED' so:
        1. Unique episode constraints are freed.
        2. Channel ingest won't reject incoming shares as duplicate.
        3. Radar and scout will detect the missing episodes and re-scout / re-download them.
        Also synchronizes watchlist.db collected_episodes.
        """
        import asyncpg
        try:
            phys_inv = await cls.get_physical_inventory()
            if not phys_inv:
                logger.info("Physical inventory is empty; skipping physical reconciliation to prevent accidental mass purge.")
                return {"success": True, "purged_count": 0, "purged_items": []}

            conn = await asyncpg.connect(PG_DSN)
            
            active_jobs = await conn.fetch("""
                SELECT DISTINCT j.title, j.season 
                FROM transfer_queue_tasks q 
                JOIN channel_ingest_jobs j ON j.id = q.reference_id
                WHERE q.status IN ('PENDING', 'RUNNING') AND j.title IS NOT NULL
            """)
            active_shows = {
                (re.sub(r'[^\w\u4e00-\u9fa5]', '', str(r['title'] or '')), int(r['season'] or 1))
                for r in active_jobs
            }

            rows = await conn.fetch("""
                SELECT r.id, t.id as task_id, t.title, COALESCE(r.season, t.season, 1) as season, r.episode, r.created_at
                FROM resources r
                JOIN tasks t ON t.id = r.task_id
                WHERE r.status = 'ACCEPTED' 
                  AND t.media_type != 'MOVIE' 
                  AND r.episode IS NOT NULL
                  AND r.created_at < (NOW() - ($1 || ' minutes')::interval)
                ORDER BY t.title, r.episode
            """, str(max_age_minutes))

            purged_ids = []
            purged_items = []
            reconciled_shows = set()
            for r in rows:
                raw_title = str(r['title'] or '').strip()
                clean_t = re.sub(r'[^\w\u4e00-\u9fa5]', '', raw_title)
                s = int(r['season'] or 1)
                ep = int(r['episode'])
                key = (clean_t, s)
                if key in active_shows:
                    continue
                if key in phys_inv:
                    # Show folder exists on physical disk!
                    if ep not in phys_inv[key]:
                        # File was deleted from physical disk!
                        purged_ids.append(r['id'])
                        purged_items.append({
                            "id": r['id'],
                            "title": raw_title,
                            "season": s,
                            "episode": ep
                        })
                        reconciled_shows.add((raw_title, clean_t, s))

            if purged_ids:
                await conn.execute("""
                    UPDATE resources
                    SET status = 'DELETED',
                        reject_reason = '物理网盘文件已删除，实盘核销释放重新转存'
                    WHERE id = ANY($1::int[])
                """, purged_ids)
                logger.info("Reconciled and purged %d deleted resource records from PG media_bot_db: %s", len(purged_ids), [(it['title'], it['season'], it['episode']) for it in purged_items[:10]])

            await conn.close()

            if reconciled_shows:
                def _sync_watchlist_sync():
                    conn_w = _open_local_db()
                    cw = conn_w.cursor()
                    for title, clean_t, s in reconciled_shows:
                        current_real_eps = sorted(list(phys_inv.get((clean_t, s), set())))
                        cw.execute("""
                            UPDATE watchlist
                            SET collected_episodes = ?
                            WHERE (title = ? OR title LIKE ?) AND season = ?
                        """, (json.dumps(current_real_eps), title, f"%{clean_t}%", s))
                    conn_w.commit()
                    conn_w.close()
                await asyncio.to_thread(_sync_watchlist_sync)

            return {
                "success": True,
                "purged_count": len(purged_ids),
                "purged_items": purged_items,
            }
        except Exception as e:
            logger.exception("Error in reconcile_physical_inventory: %s", e)
            return {"success": False, "error": str(e), "purged_count": 0}

    @classmethod
    async def get_physical_inventory(cls) -> Dict[Tuple[str, int], Set[int]]:
        """
        Returns mapping: (clean_title, season) -> Set[episode_numbers]
        from actual cloud disk files.
        """
        def _query_sync():
            conn = _open_local_db()
            c = conn.cursor()
            rows = c.execute("SELECT clean_title, season, episode FROM cloud_disk_inventory").fetchall()
            conn.close()
            res = {}
            for ct, s, ep in rows:
                res.setdefault((ct, s), set()).add(ep)
            return res

        return await asyncio.to_thread(_query_sync)

    @classmethod
    async def get_scan_meta(cls) -> Dict[str, Any]:
        def _query_meta():
            conn = _open_local_db()
            c = conn.cursor()
            rows = c.execute("SELECT key, val FROM cloud_scan_meta").fetchall()
            conn.close()
            return dict(rows)
        return await asyncio.to_thread(_query_meta)
