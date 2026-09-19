import asyncio
import aiohttp
import asyncpg
import re
from library_service import LibraryService

PG_DSN = "postgresql://mediabot:mediabot_secret_pass@127.0.0.1:5432/media_bot_db"
TMDB_KEY = os.getenv("TMDB_API_KEY", "")

async def auto_bind():
    conn = await asyncpg.connect(PG_DSN)
    tasks = await conn.fetch("SELECT id, title, media_type, season, total_episodes, tmdb_id FROM tasks WHERE media_type != 'MOVIE' ORDER BY id")
    
    async with aiohttp.ClientSession() as s:
        for t in tasks:
            t_id = t["id"]
            title = t["title"]
            season = t["season"] or 1
            tid = t["tmdb_id"]
            
            # Clean title
            clean_t = re.sub(r'^(?:追新转存|追新|转存|求片|投稿)\s*', '', title).strip()
            clean_t = re.sub(r'\s*S\d*$', '', clean_t).strip()
            
            if not tid or tid in (84958, 312523): # 84958 was Loki, 312523 was S&X for 心生
                # Search TMDB
                url = "https://api.themoviedb.org/3/search/tv"
                params = {"api_key": TMDB_KEY, "query": clean_t, "language": "zh-CN"}
                async with s.get(url, params=params) as r:
                    if r.status == 200:
                        data = await r.json()
                        results = data.get("results") or []
                        if results:
                            best = results[0]
                            new_tid = best["id"]
                            new_name = best["name"]
                            print(f"Binding Task {t_id} '{title}' -> TMDB {new_tid} ({new_name})")
                            await conn.execute("UPDATE tasks SET tmdb_id = $1 WHERE id = $2", new_tid, t_id)
                        else:
                            print(f"Could not find TMDB for Task {t_id} '{title}'")
    await conn.close()

asyncio.run(auto_bind())
