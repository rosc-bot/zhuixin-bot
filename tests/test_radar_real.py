import asyncio
import sqlite3
from library_service import LibraryService

LOCAL_DB_PATH = "/app/data/watchlist.db"

async def test():
    # Load cache into library_entries
    conn = sqlite3.connect(LOCAL_DB_PATH)
    c = conn.cursor()
    cache_rows = c.execute("SELECT clean_title, season, tmdb_id, total_episodes, is_movie FROM tmdb_title_match_cache").fetchall()
    conn.close()

    cache_map = {(r[0], r[1]): (r[2], r[3], r[4]) for r in cache_rows}

    # Now get radar summary with force_refresh
    # But first, let's see how LibraryService merges this cache
    lib = await LibraryService.get_transferred_library()
    for entry in lib:
        ct = entry["clean_title"]
        sea = entry.get("season", 1)
        if (ct, sea) in cache_map:
            tid, tot, is_m = cache_map[(ct, sea)]
            if tid:
                entry["tmdb_id"] = tid
            if tot:
                entry["total_episodes"] = tot
            if is_m:
                entry["media_type"] = "MOVIE"
                entry["is_completed"] = True
            else:
                # If TV, evaluate whether it's actually completed
                eps = entry.get("episodes", [])
                if tot and len(eps) < tot:
                    entry["is_completed"] = False

    radar = await LibraryService.get_radar_summary([], follow_mode="LATEST", force_refresh=True)
    missing = radar.get("missing_in_library", [])
    print(f"\n================ 真实缺集剧集 (LATEST 模式, 共 {len(missing)} 部) ================")
    for idx, m in enumerate(missing, start=1):
        print(f"{idx}. 《{m['title']}》第 {m['season']} 季 | 库中已存 {len(m['episodes'])} 集 | 缺集: {m['missing_episodes']} | 原因: {m['reason_text']}")

asyncio.run(test())
