import asyncio
import sqlite3
from library_service import LibraryService

LOCAL_DB_PATH = "/app/data/watchlist.db"

async def test_all_shows():
    # Connect and load cache
    conn = sqlite3.connect(LOCAL_DB_PATH)
    c = conn.cursor()
    cache_rows = c.execute("SELECT clean_title, season, tmdb_id, total_episodes, is_movie FROM tmdb_title_match_cache").fetchall()
    conn.close()
    cache_map = {(r[0], r[1]): (r[2], r[3], r[4]) for r in cache_rows}

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
                eps = entry.get("episodes", [])
                if tot and len(eps) < tot:
                    entry["is_completed"] = False

    # Prefetch TMDB metadata
    task_pairs = [(e["tmdb_id"], e["season"]) for e in lib if e.get("tmdb_id")]
    await LibraryService.prefetch_all_tmdb_meta(task_pairs)

    print("Checking each show for missing episodes against TMDB...")
    from datetime import datetime, timezone, timedelta
    beijing_tz = timezone(timedelta(hours=8))
    today_str = datetime.now(beijing_tz).date().isoformat()

    missing_list = []
    for entry in lib:
        if entry.get("media_type") == "MOVIE":
            continue
        cur_season = entry.get("season") or 1
        collected = entry.get("episodes") or []
        tmdb_id = entry.get("tmdb_id")
        title = entry.get("title")
        
        if not tmdb_id:
            print(f"⚠️ No TMDB ID for: {title} S{cur_season}")
            continue

        season_eps = await LibraryService.get_tmdb_season_episodes(tmdb_id, cur_season)
        aired_numbers = []
        future_numbers = []
        for ep in season_eps:
            ep_num = ep.get("episode_number")
            if not ep_num:
                continue
            air_d = ep.get("air_date")
            if air_d and air_d <= today_str:
                aired_numbers.append(ep_num)
            else:
                future_numbers.append(ep_num)

        if not aired_numbers:
            # Fallback
            tot = entry.get("total_episodes") or len(collected)
            aired_numbers = list(range(1, tot + 1))

        real_missing_aired = [e for e in aired_numbers if e not in collected]
        if real_missing_aired:
            missing_list.append({
                "title": title,
                "season": cur_season,
                "collected": len(collected),
                "aired_total": len(aired_numbers),
                "tmdb_total": len(season_eps),
                "missing": real_missing_aired,
                "future": future_numbers
            })

    print(f"\n🎯 Total shows with real missing episodes: {len(missing_list)}")
    for m in missing_list:
        print(f"• 《{m['title']}》S{m['season']}: 库中存 {m['collected']} 集 / 已播出 {m['aired_total']} 集 (季总共 {m['tmdb_total']} 集) | 缺集: {m['missing'][:10]} {'...' if len(m['missing'])>10 else ''} (待播: {len(m['future'])}集)")

asyncio.run(test_all_shows())
