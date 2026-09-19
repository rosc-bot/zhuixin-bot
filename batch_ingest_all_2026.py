import asyncio, json, logging, os, sys, time

sys.path.insert(0, '/app')
os.chdir('/app')

from services.scout_service import ScoutService

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

async def run_batch():
    with open('/tmp/found_guangya_2026.json') as f:
        items = json.load(f)
        
    logger.info(f"Loaded {len(items)} 2026 items ready for ingestion pipeline.")
    
    success_count = 0
    fail_count = 0
    
    for idx, item in enumerate(items, 1):
        cid = item.get("id")
        title = item.get("title", "")
        # Get actual guangya link from list
        gy_links = item.get("guangya_links") or []
        share_url = gy_links[0] if gy_links else item.get("share_url", "")
        category = item.get("category_name", "") or item.get("category", "")
        
        if not share_url:
            logger.warning(f"[{idx}/{len(items)}] Skipping {title}: no valid share_url")
            continue

        logger.info(f"[{idx}/{len(items)}] Ingesting {title} [{category}] ({share_url})...")
        try:
            res = await ScoutService.push_to_tg_media_bot(
                title=title,
                season=1,
                episodes=[],
                share_url=share_url,
                text_context=f"帧影2026全量打捞 | 分类: {category}",
            )
            if res.get("success"):
                success_count += 1
                logger.info(f"-> SUCCESS: {title} (Total Success: {success_count})")
            else:
                fail_count += 1
                logger.warning(f"-> FAILED: {title} res={res}")
        except Exception as e:
            fail_count += 1
            logger.error(f"-> ERROR ingesting {title}: {e}")
            
        await asyncio.sleep(0.5)

if __name__ == "__main__":
    asyncio.run(run_batch())
