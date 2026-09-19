import asyncio
import aiohttp
import sqlite3
import re
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))
today_str = datetime.now(BEIJING_TZ).date().isoformat()
LOCAL_DB_PATH = "/app/data/watchlist.db"
TMDB_KEY = os.getenv("TMDB_API_KEY", "")

async def check_all_95():
    conn = sqlite3.connect(LOCAL_DB_PATH)
    c = conn.cursor()
    rows = c.execute("SELECT clean_title, season, count(*), min(episode), max(episode) FROM cloud_disk_inventory GROUP BY clean_title, season ORDER BY clean_title").fetchall()
    
    # Also load tmdb_title_match_cache
    raw_m = c.execute("SELECT clean_title, season, tmdb_id, total_episodes, is_movie FROM tmdb_title_match_cache").fetchall()
    matches = {(r[0], r[1]): r for r in raw_m}
    conn.close()

    print(f"Analyzing {len(rows)} physical items against TMDB (today={today_str})...")

    async with aiohttp.ClientSession() as session:
        for clean_t, sea, cnt, min_ep, max_ep in rows:
            # 1. Check if movie
            # Search TMDB for tv and movie
            search_url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_KEY}&language=zh-CN&query={clean_t}"
            try:
                async with session.get(search_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
                    results = data.get("results") or []
            except Exception as e:
                print(f"Error searching {clean_t}: {e}")
                continue

            if not results:
                print(f"❓ [{clean_t}] S{sea}: No TMDB search result (local: {cnt} eps, E{min_ep}-E{max_ep})")
                continue

            # Pick best match
            best_tv = next((r for r in results if r.get("media_type") == "tv"), None)
            best_movie = next((r for r in results if r.get("media_type") == "movie"), None)

            if not best_tv and best_movie:
                # Movie
                continue

            target_res = best_tv if best_tv else results[0]
            if target_res.get("media_type") == "movie" and cnt == 1:
                # Movie
                continue

            tid = target_res.get("id")
            title_name = target_res.get("name") or target_res.get("title")

            # Get TV details
            tv_url = f"https://api.themoviedb.org/3/tv/{tid}?api_key={TMDB_KEY}&language=zh-CN"
            try:
                async with session.get(tv_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    tv_data = await resp.json()
            except Exception:
                continue

            # Check season
            seasons = {s.get("season_number"): s for s in (tv_data.get("seasons") or []) if s.get("season_number") is not None}
            s_info = seasons.get(sea)
            if not s_info:
                # Maybe S1
                s_info = seasons.get(1)
                matched_sea = 1
            else:
                matched_sea = sea

            # Fetch season episodes
            sea_url = f"https://api.themoviedb.org/3/tv/{tid}/season/{matched_sea}?api_key={TMDB_KEY}&language=zh-CN"
            try:
                async with session.get(sea_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    sea_data = await resp.json()
                    episodes = sea_data.get("episodes") or []
            except Exception:
                episodes = []

            # Determine aired episodes
            aired_eps = []
            future_eps = []
            for ep in episodes:
                ep_num = ep.get("episode_number")
                air_d = ep.get("air_date")
                if air_d and air_d <= today_str:
                    aired_eps.append(ep_num)
                elif air_d and air_d > today_str:
                    future_eps.append(ep_num)
                else:
                    if tv_data.get("status") in ("Ended", "Canceled"):
                        aired_eps.append(ep_num)
                    elif ep_num <= max_ep:
                        aired_eps.append(ep_num)
                    else:
                        future_eps.append(ep_num)

            max_aired = max(aired_eps) if aired_eps else (len(episodes) or max_ep)

            # Compare with local
            # Let's get actual local episodes list
            conn = sqlite3.connect(LOCAL_DB_PATH)
            local_eps = sorted([r[0] for r in conn.execute("SELECT episode FROM cloud_disk_inventory WHERE clean_title = ? AND season = ?", (clean_t, sea)).fetchall()])
            conn.close()

            missing = [e for e in aired_eps if e not in local_eps]
            trailing_new = [e for e in missing if e > max(local_eps) if local_eps]
            early_missing = [e for e in missing if local_eps and e < min(local_eps)]
            gap_missing = [e for e in missing if local_eps and min(local_eps) < e < max(local_eps)]

            is_ended = tv_data.get("status") in ("Ended", "Canceled")
            status_desc = "已完结" if is_ended else f"连载中({tv_data.get('status')})"

            if trailing_new:
                print(f"🔥 【追新待收】《{clean_t}》S{sea} [{title_name}]: 库中E{min_ep}-E{max_ep}(共{cnt}集) | 官方已播至E{max_aired} | 🔥待收新集: {trailing_new} | 状态: {status_desc}")
            elif gap_missing:
                print(f"⚠️ 【中间缺集】《{clean_t}》S{sea} [{title_name}]: 库中E{min_ep}-E{max_ep} | 缺断集: {gap_missing} | 状态: {status_desc}")
            elif early_missing and not is_ended:
                print(f"⏳ 【连载中·前缺】《{clean_t}》S{sea} [{title_name}]: 库中E{min_ep}-E{max_ep} | 缺前置: E01-E{max(early_missing)} | 状态: {status_desc}")
            elif missing:
                print(f"📦 【完结补齐】《{clean_t}》S{sea} [{title_name}]: 库中{cnt}集 | 缺集: {len(missing)}集 | 状态: {status_desc}")
            else:
                if not is_ended:
                    next_ep_info = f"下一集E{min(future_eps)}待播" if future_eps else "等待定档"
                    print(f"✅ 【连载已跟最新】《{clean_t}》S{sea} [{title_name}]: 库中至E{max_ep} = 官方已播E{max_aired} | 状态: {status_desc} ({next_ep_info})")

if __name__ == "__main__":
    asyncio.run(check_all_95())
