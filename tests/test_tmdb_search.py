import asyncio
import aiohttp
import re
import urllib.parse
from library_service import LibraryService

TMDB_KEY = os.getenv("TMDB_API_KEY", "")

async def test_search():
    lib = await LibraryService.get_transferred_library()
    no_tmdb = [e for e in lib if not e.get("tmdb_id") and e.get("media_type") != "MOVIE"]
    print(f"Total TV series without TMDB ID: {len(no_tmdb)}")

    async with aiohttp.ClientSession() as s:
        for item in no_tmdb:
            t = item["title"]
            clean_t = item["clean_title"]
            sea = item.get("season", 1)
            coll = item.get("episodes", [])
            
            # search
            query = clean_t or t
            url = "https://api.themoviedb.org/3/search/tv"
            params = {"api_key": TMDB_KEY, "query": query, "language": "zh-CN"}
            async with s.get(url, params=params) as r:
                if r.status == 200:
                    data = await r.json()
                    res = data.get("results") or []
                    if res:
                        best = res[0]
                        b_id = best.get("id")
                        b_name = best.get("name")
                        b_seasons = best.get("number_of_seasons")
                        
                        # fetch season details
                        s_url = f"https://api.themoviedb.org/3/tv/{b_id}"
                        async with s.get(s_url, params={"api_key": TMDB_KEY, "language": "zh-CN"}) as sr:
                            sdata = await sr.json()
                            s_map = {sn.get("season_number"): sn for sn in (sdata.get("seasons") or [])}
                            s_info = s_map.get(sea)
                            if s_info:
                                s_eps = s_info.get("episode_count")
                                print(f"🔍 {t} S{sea}: Matched TMDB {b_id} ({b_name}) S{sea} has {s_eps} eps! Local has {len(coll)} eps ({coll[:3]}..{coll[-3:] if len(coll)>3 else ''})")
                            else:
                                print(f"❓ {t} S{sea}: Matched {b_name} but no S{sea} (has {list(s_map.keys())})")
                    else:
                        print(f"❌ {t} S{sea}: TMDB search returned 0 results")

asyncio.run(test_search())
