import asyncio
import aiohttp
import re
import sqlite3
from library_service import LibraryService

LOCAL_DB_PATH = "/app/data/watchlist.db"
TMDB_KEY = os.getenv("TMDB_API_KEY", "")

def init_match_cache():
    conn = sqlite3.connect(LOCAL_DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tmdb_title_match_cache (
            clean_title TEXT NOT NULL,
            season INTEGER NOT NULL DEFAULT 1,
            tmdb_id INTEGER,
            tmdb_title TEXT,
            total_episodes INTEGER,
            is_movie INTEGER DEFAULT 0,
            PRIMARY KEY(clean_title, season)
        )
    """)
    conn.commit()
    conn.close()

async def resolve_tmdb_for_shows():
    init_match_cache()
    lib = await LibraryService.get_transferred_library()
    print(f"Total lib entries: {len(lib)}")

    conn = sqlite3.connect(LOCAL_DB_PATH)
    c = conn.cursor()

    async with aiohttp.ClientSession() as session:
        for entry in lib:
            clean_t = entry["clean_title"]
            sea = entry.get("season", 1)
            tid = entry.get("tmdb_id")

            # Check cache
            row = c.execute("SELECT tmdb_id, total_episodes, is_movie FROM tmdb_title_match_cache WHERE clean_title = ? AND season = ?", (clean_t, sea)).fetchone()
            if row and row[0]:
                entry["tmdb_id"] = row[0]
                entry["total_episodes"] = row[1]
                if row[2]:
                    entry["media_type"] = "MOVIE"
                continue

            if tid:
                # Cache existing
                c.execute("INSERT OR REPLACE INTO tmdb_title_match_cache (clean_title, season, tmdb_id, total_episodes) VALUES (?, ?, ?, ?)",
                          (clean_t, sea, tid, entry.get("total_episodes")))
                conn.commit()
                continue

            # Need to search TMDB!
            # Search TV first
            url = "https://api.themoviedb.org/3/search/tv"
            params = {"api_key": TMDB_KEY, "query": clean_t, "language": "zh-CN"}
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("results") or []
                        matched = False
                        if results:
                            best = results[0]
                            b_id = best["id"]
                            b_name = best.get("name") or best.get("original_name")
                            
                            # Get seasons info
                            s_url = f"https://api.themoviedb.org/3/tv/{b_id}"
                            async with session.get(s_url, params={"api_key": TMDB_KEY, "language": "zh-CN"}, timeout=aiohttp.ClientTimeout(total=8)) as sr:
                                if sr.status == 200:
                                    sdata = await sr.json()
                                    seasons_map = {sn.get("season_number"): sn for sn in (sdata.get("seasons") or [])}
                                    s_info = seasons_map.get(sea)
                                    if s_info:
                                        tot = s_info.get("episode_count") or 0
                                        entry["tmdb_id"] = b_id
                                        entry["total_episodes"] = tot
                                        c.execute("INSERT OR REPLACE INTO tmdb_title_match_cache (clean_title, season, tmdb_id, tmdb_title, total_episodes, is_movie) VALUES (?, ?, ?, ?, ?, 0)",
                                                  (clean_t, sea, b_id, b_name, tot))
                                        conn.commit()
                                        print(f"✅ Matched TV: {clean_t} S{sea} -> TMDB {b_id} ({b_name}) {tot} eps")
                                        matched = True
                        if not matched and len(entry.get("episodes", [])) == 1:
                            # Try Movie search
                            m_url = "https://api.themoviedb.org/3/search/movie"
                            async with session.get(m_url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as mr:
                                if mr.status == 200:
                                    mdata = await mr.json()
                                    mres = mdata.get("results") or []
                                    if mres:
                                        mb = mres[0]
                                        mb_id = mb["id"]
                                        mb_name = mb.get("title") or mb.get("original_title")
                                        entry["tmdb_id"] = mb_id
                                        entry["media_type"] = "MOVIE"
                                        entry["total_episodes"] = 1
                                        c.execute("INSERT OR REPLACE INTO tmdb_title_match_cache (clean_title, season, tmdb_id, tmdb_title, total_episodes, is_movie) VALUES (?, ?, ?, ?, 1, 1)",
                                                  (clean_t, sea, mb_id, mb_name))
                                        conn.commit()
                                        print(f"🎬 Matched Movie: {clean_t} -> TMDB {mb_id} ({mb_name})")
                                        matched = True
                        if not matched:
                            print(f"⚠️ Unmatched: {clean_t} S{sea} (local eps: {len(entry.get('episodes', []))})")
            except Exception as e:
                print(f"Error searching TMDB for {clean_t}: {e}")

    conn.close()

asyncio.run(resolve_tmdb_for_shows())
