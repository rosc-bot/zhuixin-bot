import re
import json
import sqlite3
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from config import LOCAL_DB_PATH

logger = logging.getLogger(__name__)



import sqlite3


def _open_local_db(timeout: float = 10.0):
    """统一 SQLite 连接工厂：设置 timeout + busy_timeout，避免 'database is locked'。"""
    conn = sqlite3.connect(LOCAL_DB_PATH, timeout=timeout)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
    except Exception:
        pass
    return conn

def _init_db_sync():
    conn = _open_local_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tmdb_id INTEGER,
            title TEXT NOT NULL,
            season INTEGER NOT NULL DEFAULT 1,
            total_episodes INTEGER,
            last_aired_episode INTEGER DEFAULT 0,
            collected_episodes TEXT DEFAULT '[]',
            poster_url TEXT,
            source TEXT DEFAULT 'sztv',
            status TEXT DEFAULT 'FOLLOWING',
            user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            follow_mode TEXT DEFAULT 'LATEST',
            start_from_episode INTEGER,
            UNIQUE(title, season)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS ignored_missing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            season INTEGER NOT NULL DEFAULT 1,
            episode INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(title, season, episode)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            val TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS auto_ingest_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            season INTEGER NOT NULL,
            episodes TEXT,
            share_url TEXT,
            provider TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(title, season, share_url)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS failed_scout_pushes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            season INTEGER NOT NULL DEFAULT 1,
            episodes TEXT,
            share_url TEXT NOT NULL,
            provider TEXT,
            text_context TEXT,
            error_message TEXT,
            status TEXT DEFAULT 'FAILED',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Default settings
    c.execute("INSERT OR IGNORE INTO bot_settings (key, val) VALUES ('auto_ingest_enabled', '1')")
    c.execute("INSERT OR IGNORE INTO bot_settings (key, val) VALUES ('auto_ingest_categories', 'domestic,anime,western,jp-kr,movie')")
    c.execute("INSERT OR IGNORE INTO bot_settings (key, val) VALUES ('auto_ingest_mode', 'LATEST')")
    conn.commit()
    conn.close()

async def init_db():
    await asyncio.to_thread(_init_db_sync)

def _add_or_update_sync(
    title: str,
    season: int,
    tmdb_id,
    total_episodes,
    last_aired_episode,
    poster_url,
    user_id,
    source,
    follow_mode="LATEST",
    start_from_episode=None
):
    clean_title = title.strip()
    conn = _open_local_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    row = c.execute("SELECT * FROM watchlist WHERE title = ? AND season = ?", (clean_title, season)).fetchone()
    if row:
        item_id = row["id"]
        c.execute("""
            UPDATE watchlist
            SET tmdb_id = COALESCE(?, tmdb_id),
                total_episodes = COALESCE(?, total_episodes),
                last_aired_episode = MAX(last_aired_episode, ?),
                poster_url = COALESCE(?, poster_url),
                status = 'FOLLOWING',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (tmdb_id, total_episodes, last_aired_episode, poster_url, item_id))
        conn.commit()
    else:
        c.execute("""
            INSERT INTO watchlist (title, season, tmdb_id, total_episodes, last_aired_episode, poster_url, user_id, source, follow_mode, start_from_episode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (clean_title, season, tmdb_id, total_episodes, last_aired_episode, poster_url, user_id, source, follow_mode, start_from_episode))
        conn.commit()

    row = c.execute("SELECT * FROM watchlist WHERE title = ? AND season = ?", (clean_title, season)).fetchone()
    res = dict(row)
    res["collected_episodes"] = json.loads(res.get("collected_episodes") or "[]")
    conn.close()
    return res

async def add_or_update_watchlist(
    title: str,
    season: int = 1,
    tmdb_id: Optional[int] = None,
    total_episodes: Optional[int] = None,
    last_aired_episode: int = 0,
    poster_url: Optional[str] = None,
    user_id: Optional[int] = None,
    source: str = "sztv",
    follow_mode: str = "LATEST",
    start_from_episode: Optional[int] = None,
) -> Dict[str, Any]:
    return await asyncio.to_thread(
        _add_or_update_sync,
        title, season, tmdb_id, total_episodes, last_aired_episode, poster_url, user_id, source, follow_mode, start_from_episode
    )

def _get_all_following_sync(user_id: Optional[int] = None):
    conn = _open_local_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    query = "SELECT * FROM watchlist WHERE status = 'FOLLOWING'"
    params = []
    if user_id:
        query += " AND (user_id = ? OR user_id IS NULL)"
        params.append(user_id)
    query += " ORDER BY updated_at DESC, id DESC"
    rows = c.execute(query, params).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["collected_episodes"] = json.loads(d.get("collected_episodes") or "[]")
        items.append(d)
    conn.close()
    return items

async def get_all_following(user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(_get_all_following_sync, user_id)

def _get_by_id_sync(item_id: int):
    conn = _open_local_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    row = c.execute("SELECT * FROM watchlist WHERE id = ?", (item_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["collected_episodes"] = json.loads(d.get("collected_episodes") or "[]")
    return d

async def get_watchlist_by_id(item_id: int) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(_get_by_id_sync, item_id)

def _remove_sync(item_id: int) -> bool:
    conn = _open_local_db()
    c = conn.cursor()
    c.execute("DELETE FROM watchlist WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return True

async def remove_from_watchlist(item_id: int) -> bool:
    return await asyncio.to_thread(_remove_sync, item_id)

def _toggle_follow_mode_sync(item_id: int) -> str:
    conn = _open_local_db()
    c = conn.cursor()
    row = c.execute("SELECT follow_mode FROM watchlist WHERE id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return "LATEST"
    curr = row[0] or "LATEST"
    new_mode = "FULL" if curr == "LATEST" else "LATEST"
    c.execute("UPDATE watchlist SET follow_mode = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_mode, item_id))
    conn.commit()
    conn.close()
    return new_mode

async def toggle_follow_mode(item_id: int) -> str:
    return await asyncio.to_thread(_toggle_follow_mode_sync, item_id)

def _set_follow_mode_sync(item_id: int, mode: str, start_ep: Optional[int] = None) -> bool:
    conn = _open_local_db()
    c = conn.cursor()
    c.execute(
        "UPDATE watchlist SET follow_mode = ?, start_from_episode = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (mode, start_ep, item_id)
    )
    conn.commit()
    conn.close()
    return True

async def set_follow_mode(item_id: int, mode: str, start_ep: Optional[int] = None) -> bool:
    return await asyncio.to_thread(_set_follow_mode_sync, item_id, mode, start_ep)

def _mark_collected_sync(item_id: int, episodes: List[int]):
    conn = _open_local_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    row = c.execute("SELECT collected_episodes FROM watchlist WHERE id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return []
    current = set(json.loads(row[0] or "[]"))
    current.update(episodes)
    updated_list = sorted(list(current))
    c.execute(
        "UPDATE watchlist SET collected_episodes = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (json.dumps(updated_list), item_id)
    )
    conn.commit()
    conn.close()
    return updated_list

async def mark_episodes_collected(item_id: int, episodes: List[int]) -> List[int]:
    return await asyncio.to_thread(_mark_collected_sync, item_id, episodes)

# --- Ignored Missing Episodes Storage ---

def _add_ignored_missing_sync(title: str, season: int, episode: int = 0):
    clean_title = title.strip()
    conn = _open_local_db()
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO ignored_missing (title, season, episode)
        VALUES (?, ?, ?)
    """, (clean_title, season, int(episode)))
    conn.commit()
    conn.close()

async def add_ignored_missing(title: str, season: int, episode: int = 0):
    await asyncio.to_thread(_add_ignored_missing_sync, title, season, episode)

def _remove_ignored_missing_sync(title: str, season: int, episode: Optional[int] = None):
    clean_title = title.strip()
    conn = _open_local_db()
    c = conn.cursor()
    if episode is None:
        c.execute("DELETE FROM ignored_missing WHERE title = ? AND season = ?", (clean_title, season))
    else:
        c.execute("DELETE FROM ignored_missing WHERE title = ? AND season = ? AND episode = ?", (clean_title, season, int(episode)))
    conn.commit()
    conn.close()

async def remove_ignored_missing(title: str, season: int, episode: Optional[int] = None):
    await asyncio.to_thread(_remove_ignored_missing_sync, title, season, episode)

def _clear_all_ignored_sync():
    conn = _open_local_db()
    c = conn.cursor()
    c.execute("DELETE FROM ignored_missing")
    conn.commit()
    conn.close()

async def clear_all_ignored():
    await asyncio.to_thread(_clear_all_ignored_sync)

def _get_all_ignored_rules_sync() -> Dict[Tuple[str, int], Set[int]]:
    conn = _open_local_db()
    c = conn.cursor()
    rows = c.execute("SELECT title, season, episode FROM ignored_missing").fetchall()
    conn.close()
    rules: Dict[Tuple[str, int], Set[int]] = {}
    for t, s, ep in rows:
        clean_t = re.sub(r'[^\w\u4e00-\u9fa5]', '', t)
        rules.setdefault((t, s), set()).add(int(ep))
        rules.setdefault((clean_t, s), set()).add(int(ep))
    return rules

async def get_all_ignored_rules() -> Dict[Tuple[str, int], Set[int]]:
    return await asyncio.to_thread(_get_all_ignored_rules_sync)

def _list_all_ignored_sync() -> List[Dict[str, Any]]:
    conn = _open_local_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    rows = c.execute("SELECT * FROM ignored_missing ORDER BY created_at DESC, id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

async def list_all_ignored() -> List[Dict[str, Any]]:
    return await asyncio.to_thread(_list_all_ignored_sync)



# --- Bot Settings & Auto-Ingest Storage ---

def _get_setting_sync(key: str, default: str = "") -> str:
    conn = _open_local_db()
    c = conn.cursor()
    row = c.execute("SELECT val FROM bot_settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default

def _set_setting_sync(key: str, val: str):
    conn = _open_local_db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO bot_settings (key, val, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (key, str(val)))
    conn.commit()
    conn.close()

def _record_auto_ingest_sync(title: str, season: int, episodes: List[int], share_url: str, provider: str):
    conn = _open_local_db()
    c = conn.cursor()
    existing = c.execute(
        "SELECT id, episodes FROM auto_ingest_history WHERE title = ? AND season = ? AND share_url = ?",
        (title, season, share_url),
    ).fetchone()
    try:
        old_episodes = json.loads(existing[1] or "[]") if existing else []
    except (TypeError, ValueError, json.JSONDecodeError):
        old_episodes = []
    merged = set()
    for value in list(old_episodes or []) + list(episodes or []):
        try:
            episode = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if 1 <= episode <= 9999:
            merged.add(episode)
    if existing:
        c.execute(
            """
            UPDATE auto_ingest_history
               SET episodes = ?, provider = ?, created_at = CURRENT_TIMESTAMP
             WHERE id = ?
            """,
            (json.dumps(sorted(merged)), provider, existing[0]),
        )
    else:
        c.execute(
            """
            INSERT INTO auto_ingest_history (title, season, episodes, share_url, provider)
            VALUES (?, ?, ?, ?, ?)
            """,
            (title, season, json.dumps(sorted(merged)), share_url, provider),
        )
    conn.commit()
    conn.close()


def _is_auto_ingested_sync(
    title: str,
    season: int,
    share_url: str,
    episodes: Optional[List[int]] = None,
) -> bool:
    conn = _open_local_db()
    c = conn.cursor()
    row = c.execute(
        "SELECT episodes FROM auto_ingest_history WHERE title = ? AND season = ? AND share_url = ?",
        (title, season, share_url),
    ).fetchone()
    conn.close()
    if not row:
        return False
    if not episodes:
        return True
    try:
        recorded = {
            int(value)
            for value in (json.loads(row[0] or "[]") or [])
            if str(value).strip().isdigit()
        }
        requested = {
            int(value)
            for value in episodes
            if str(value).strip().isdigit()
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(requested) and requested.issubset(recorded)


def _get_auto_ingest_history_sync(limit: int = 20) -> List[Dict[str, Any]]:
    conn = _open_local_db()
    c = conn.cursor()
    rows = c.execute("""
        SELECT id, title, season, episodes, share_url, provider, created_at
        FROM auto_ingest_history
        ORDER BY id DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    res = []
    for r in rows:
        import json
        try:
            eps = json.loads(r[3])
        except Exception:
            eps = []
        res.append({
            "id": r[0],
            "title": r[1],
            "season": r[2],
            "episodes": eps,
            "share_url": r[4],
            "provider": r[5],
            "created_at": r[6]
        })
    return res


def _get_historical_share_urls_sync(title: str, season: int) -> List[str]:
    try:
        conn = _open_local_db()
        c = conn.cursor()
        clean_t = re.sub(r'[^\w\u4e00-\u9fa5]', '', title)
        rows = c.execute("""
            SELECT DISTINCT share_url FROM auto_ingest_history 
            WHERE (title = ? OR title LIKE ?) AND season = ? AND share_url IS NOT NULL
            ORDER BY id DESC LIMIT 5
        """, (title, f"%{clean_t}%", season)).fetchall()
        conn.close()
        return [r[0] for r in rows if r[0]]
    except Exception as e:
        logger.warning("Error fetching historical share urls: %s", e)
        return []




def _record_failed_scout_push_sync(title: str, season: int, episodes: List[int], share_url: str, provider: str, text_context: str, error_message: str) -> int:
    conn = _open_local_db()
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO failed_scout_pushes (title, season, episodes, share_url, provider, text_context, error_message, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'FAILED')
    """, (title, season, json.dumps(episodes), share_url, provider, text_context, error_message))
    push_id = c.lastrowid
    conn.commit()
    conn.close()
    return push_id

def _get_failed_scout_push_sync(push_id: int) -> Optional[Dict[str, Any]]:
    conn = _open_local_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    row = c.execute("SELECT * FROM failed_scout_pushes WHERE id = ?", (push_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    if d.get("episodes"):
        try:
            d["episodes"] = json.loads(d["episodes"])
        except Exception:
            d["episodes"] = []
    return d

def _update_failed_scout_push_sync(push_id: int, status: str, error_message: Optional[str] = None):
    conn = _open_local_db()
    c = conn.cursor()
    if error_message is not None:
        c.execute("UPDATE failed_scout_pushes SET status = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, error_message, push_id))
    else:
        c.execute("UPDATE failed_scout_pushes SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, push_id))
    conn.commit()
    conn.close()
def _sync_ongoing_show_to_watchlist_sync(
    title: str,
    season: int,
    tmdb_id: Optional[int],
    total_episodes: int,
    last_aired: int,
    collected_eps: List[int],
    poster_url: Optional[str] = None,
    user_id: int = 8586984520,
    is_ongoing: bool = True
):
    conn = _open_local_db()
    conn.execute("PRAGMA busy_timeout = 5000")
    c = conn.cursor()
    # 拦截垃圾标题
    clean_t = (title or "").strip()
    if any(k in clean_t for k in ["新资源", "上架", "合集", "测试", "未命名"]):
        conn.close()
        return
    # 严禁将正在连载中(is_ongoing=True)的剧集误删！只有当明确已完结且全集收齐时才清理
    if not is_ongoing and total_episodes and total_episodes > 0 and len(collected_eps) >= total_episodes:
        c.execute("DELETE FROM watchlist WHERE (title = ? OR title = ?) AND season = ?", (title, clean_t, season))
        conn.commit()
        conn.close()
        return

    coll_json = json.dumps(sorted(list(set(collected_eps))))
    # 三层匹配：
    # 1) title + season 精确匹配
    # 2) 归一化 title (去空白/破折号 + 小写) + season 匹配 —— 只在此层才合并同剧不同写法
    # 注意：不按 tmdb_id 归并！同一 tmdb_id 可能对应多部作品（如「时光代理人 S1」与「时光代理人 英都篇 S1」共用 tmdb_id=123542）
    normalized = re.sub(r"[\s\-_]+", "", (title or "")).lower()
    row = c.execute(
        "SELECT id, status, collected_episodes, follow_mode FROM watchlist WHERE title = ? AND season = ?",
        (title, season)
    ).fetchone()
    if not row and normalized:
        rows = c.execute(
            "SELECT id, title, season, tmdb_id, status, collected_episodes, follow_mode FROM watchlist WHERE season = ?",
            (season,)
        ).fetchall()
        for r in rows:
            r_norm = re.sub(r"[\s\-_]+", "", (r[1] or "")).lower()
            if r_norm == normalized:
                row = (r[0], r[4], r[5], r[6])
                break
    if not row:
        c.execute("""
            INSERT INTO watchlist (title, season, tmdb_id, total_episodes, last_aired_episode, collected_episodes, poster_url, user_id, status, follow_mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'FOLLOWING', 'LATEST')
        """, (title, season, tmdb_id, total_episodes, last_aired, coll_json, poster_url, user_id))
    else:
        sub_id = row[0]
        c.execute("""
            UPDATE watchlist
            SET tmdb_id = COALESCE(?, tmdb_id),
                total_episodes = MAX(COALESCE(?, 0), total_episodes),
                last_aired_episode = MAX(COALESCE(?, 0), last_aired_episode),
                collected_episodes = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (tmdb_id, total_episodes, last_aired, coll_json, sub_id))
    conn.commit()
    conn.close()


class DatabaseService:
    @classmethod
    async def init_db(cls):
        await init_db()

    @classmethod
    async def list_subscriptions(cls, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        return await get_all_following(user_id)

    @classmethod
    async def add_subscription(
        cls,
        title: str,
        season: int = 1,
        tmdb_id: Optional[int] = None,
        total_episodes: Optional[int] = None,
        last_aired_episode: int = 0,
        poster_url: Optional[str] = None,
        user_id: Optional[int] = None,
        source: str = "sztv",
        status: str = "FOLLOWING",
        follow_mode: str = "LATEST",
        start_from_episode: Optional[int] = None
    ) -> Dict[str, Any]:
        return await add_or_update_watchlist(
            title=title,
            season=season,
            tmdb_id=tmdb_id,
            total_episodes=total_episodes,
            last_aired_episode=last_aired_episode,
            poster_url=poster_url,
            user_id=user_id,
            source=source,
            follow_mode=follow_mode,
            start_from_episode=start_from_episode
        )

    @classmethod
    async def toggle_follow_mode(cls, sub_id: int) -> str:
        return await toggle_follow_mode(sub_id)

    @classmethod
    async def set_follow_mode(cls, sub_id: int, mode: str, start_ep: Optional[int] = None) -> bool:
        return await set_follow_mode(sub_id, mode, start_ep)

    @classmethod
    async def delete_subscription(cls, sub_id: int) -> bool:
        return await remove_from_watchlist(sub_id)

    @classmethod
    async def get_subscription(cls, sub_id: int) -> Optional[Dict[str, Any]]:
        return await get_watchlist_by_id(sub_id)

    @classmethod
    async def mark_collected(cls, sub_id: int, episodes: List[int]) -> List[int]:
        return await mark_episodes_collected(sub_id, episodes)

    @classmethod
    async def add_ignored(cls, title: str, season: int, episode: int = 0):
        await add_ignored_missing(title, season, episode)

    @classmethod
    async def remove_ignored(cls, title: str, season: int, episode: Optional[int] = None):
        await remove_ignored_missing(title, season, episode)

    @classmethod
    async def clear_all_ignored(cls):
        await clear_all_ignored()

    @classmethod
    async def get_ignored_rules(cls) -> Dict[Tuple[str, int], Set[int]]:
        return await get_all_ignored_rules()

    @classmethod
    async def list_ignored(cls) -> List[Dict[str, Any]]:
        return await list_all_ignored()
    @classmethod
    async def get_setting(cls, key: str, default: str = "") -> str:
        return await asyncio.to_thread(_get_setting_sync, key, default)

    @classmethod
    async def set_setting(cls, key: str, val: str):
        await asyncio.to_thread(_set_setting_sync, key, val)

    @classmethod
    async def is_auto_ingest_enabled(cls) -> bool:
        v = await cls.get_setting("auto_ingest_enabled", "1")
        return v in ("1", "true", "True")

    @classmethod
    async def set_auto_ingest_enabled(cls, enabled: bool):
        await cls.set_setting("auto_ingest_enabled", "1" if enabled else "0")

    @classmethod
    async def toggle_auto_ingest_enabled(cls) -> bool:
        curr = await cls.is_auto_ingest_enabled()
        new_val = not curr
        await cls.set_auto_ingest_enabled(new_val)
        return new_val

    @classmethod
    async def get_auto_ingest_categories(cls) -> List[str]:
        val = await cls.get_setting("auto_ingest_categories", "domestic,anime,western,jp-kr,movie")
        return [x.strip() for x in val.split(",") if x.strip()]

    @classmethod
    async def toggle_auto_ingest_category(cls, cat_key: str) -> List[str]:
        cats = await cls.get_auto_ingest_categories()
        if cat_key in cats:
            cats.remove(cat_key)
        else:
            cats.append(cat_key)
        await cls.set_setting("auto_ingest_categories", ",".join(cats))
        return cats

    @classmethod
    async def record_auto_ingest(cls, title: str, season: int, episodes: List[int], share_url: str, provider: str):
        await asyncio.to_thread(_record_auto_ingest_sync, title, season, episodes, share_url, provider)

    @classmethod
    async def is_auto_ingested(
        cls,
        title: str,
        season: int,
        share_url: str,
        episodes: Optional[List[int]] = None,
    ) -> bool:
        return await asyncio.to_thread(
            _is_auto_ingested_sync, title, season, share_url, episodes
        )

    @classmethod
    async def get_auto_ingest_history(cls, limit: int = 20) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(_get_auto_ingest_history_sync, limit)

    @classmethod
    async def get_historical_share_urls(cls, title: str, season: int) -> List[str]:
        return await asyncio.to_thread(_get_historical_share_urls_sync, title, season)

    @classmethod
    async def record_failed_scout_push(cls, title: str, season: int, episodes: List[int], share_url: str, provider: str, text_context: str, error_message: str) -> int:
        return await asyncio.to_thread(_record_failed_scout_push_sync, title, season, episodes, share_url, provider, text_context, error_message)

    @classmethod
    async def get_failed_scout_push(cls, push_id: int) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(_get_failed_scout_push_sync, push_id)

    @classmethod
    async def update_failed_scout_push(cls, push_id: int, status: str, error_message: Optional[str] = None):
        await asyncio.to_thread(_update_failed_scout_push_sync, push_id, status, error_message)

    @classmethod
    async def update_failed_scout_push_status(cls, push_id: int, status: str, error_message: Optional[str] = None):
        await asyncio.to_thread(_update_failed_scout_push_sync, push_id, status, error_message)

    @classmethod
    async def sync_ongoing_to_watchlist(
        cls,
        title: str,
        season: int,
        tmdb_id: Optional[int],
        total_episodes: int,
        last_aired: int,
        collected_eps: List[int],
        poster_url: Optional[str] = None,
        user_id: int = 8586984520,
        is_ongoing: bool = True
    ):
        await asyncio.to_thread(
            _sync_ongoing_show_to_watchlist_sync,
            title, season, tmdb_id, total_episodes, last_aired, collected_eps, poster_url, user_id, is_ongoing
        )

