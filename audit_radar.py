import asyncio
from library_service import LibraryService

async def audit():
    lib = await LibraryService.get_transferred_library()
    print(f"Total entries in transferred library: {len(lib)}")
    radar = await LibraryService.get_radar_summary([], follow_mode="FULL", force_refresh=True)
    missing = radar.get("missing_in_library", [])
    completed = radar.get("completed_in_library", [])
    print(f"Missing count: {len(missing)}")
    print(f"Completed count: {len(completed)}")

    print("\n=== ALL MISSING SHOWS (FULL MODE) ===")
    for m in missing:
        print(f"❌ {m['title']} S{m['season']}: collected={len(m['episodes'])} missing={m['missing_episodes']} reason={m['reason_text']}")

    print("\n=== COMPLETED SHOWS THAT MIGHT HAVE MORE EPISODES ON TMDB ===")
    for c in completed:
        if c.get("media_type") == "MOVIE":
            continue
        coll = c.get("episodes", [])
        tot = c.get("total_episodes")
        eff = c.get("effective_target")
        tid = c.get("tmdb_id")
        print(f"✅ {c['title']} S{c['season']}: coll={len(coll)} eff={eff} tot={tot} tid={tid} ({c.get('reason_text')})")

asyncio.run(audit())
