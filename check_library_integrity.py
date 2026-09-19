import asyncio
import aiohttp
import asyncpg
import re

PG_DSN = "postgresql://mediabot:mediabot_secret_pass@127.0.0.1:5432/media_bot_db"
TMDB_KEY = os.getenv("TMDB_API_KEY", "")

async def run():
    conn = await asyncpg.connect(PG_DSN)
    tasks = await conn.fetch("SELECT id, title, media_type, season, total_episodes, tmdb_id FROM tasks WHERE media_type != 'MOVIE' ORDER BY id")
    await conn.close()

    print(f"Checking {len(tasks)} TV series tasks in DB...")
    async with aiohttp.ClientSession() as s:
        for t in tasks:
            tid = t["tmdb_id"]
            title = t["title"]
            season = t["season"] or 1
            t_id = t["id"]
            if not tid:
                print(f"[NO TMDB] Task {t_id}: '{title}' S{season}")
                continue
            url = f"https://api.themoviedb.org/3/tv/{tid}"
            params = {"api_key": TMDB_KEY, "language": "zh-CN"}
            try:
                async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status != 200:
                        print(f"[TMDB ERR {r.status}] Task {t_id}: '{title}' tmdb_id={tid}")
                        continue
                    data = await r.json()
                    tmdb_name = data.get("name") or data.get("original_name") or ""
                    seasons = {s_info.get("season_number"): s_info for s_info in (data.get("seasons") or [])}
                    
                    # Title check
                    c1 = set(re.sub(r"[^\w\u4e00-\u9fa5]", "", title))
                    c2 = set(re.sub(r"[^\w\u4e00-\u9fa5]", "", tmdb_name))
                    inter = c1 & c2
                    if len(c1) >= 2 and not inter:
                        print(f"[TITLE MISMATCH] Task {t_id}: '{title}' vs TMDB {tid} ('{tmdb_name}')")
                    
                    if season not in seasons:
                        print(f"[NO SEASON {season}] Task {t_id}: '{title}' S{season} vs TMDB {tid} ('{tmdb_name}') seasons={list(seasons.keys())}")
            except Exception as e:
                print(f"[REQ EXCEPTION] Task {t_id}: {e}")

asyncio.run(run())
