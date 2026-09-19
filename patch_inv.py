with open("/home/ubuntu/如昔项目归总/影视追新机器人/services/cloud_inventory_service.py", "r", encoding="utf-8") as f:
    text = f.read()

# 1. Update return in scan_guangya_master
old_ret = """        t1 = time.time()
        logger.info("Guangya physical cloud scan finished in %.2f s. Persisted %d files across %d titles.", t1 - t0, len(show_items), len(shows))
        return {
            "success": True,
            "scan_duration": round(t1 - t0, 2),
            "total_files": len(show_items),
            "total_titles": len(shows),
        }"""

new_ret = """        t1 = time.time()
        logger.info("Guangya physical cloud scan finished in %.2f s. Persisted %d files across %d titles.", t1 - t0, len(show_items), len(shows))
        rec_res = await cls.reconcile_physical_inventory()
        return {
            "success": True,
            "scan_duration": round(t1 - t0, 2),
            "total_files": len(show_items),
            "total_titles": len(shows),
            "purged_count": rec_res.get("purged_count", 0),
            "purged_items": rec_res.get("purged_items", []),
        }"""

assert old_ret in text, "old_ret not found in cloud_inventory_service.py"
text = text.replace(old_ret, new_ret)

# 2. Add reconcile_physical_inventory method before get_physical_inventory
old_anchor = "    @classmethod\n    async def get_physical_inventory(cls) -> Dict[Tuple[str, int], Set[int]]:"

reconcile_func = """    @classmethod
    async def reconcile_physical_inventory(cls, max_age_minutes: int = 30) -> Dict[str, Any]:
        \"\"\"
        Reconcile Postgres media_bot_db resources against physical inventory on Guangya Cloud.
        If a TV series folder exists in physical inventory on Guangya Cloud, but specific
        episodes recorded as 'ACCEPTED' in Postgres are missing from physical files (and were
        created at least max_age_minutes ago and not in an active in-flight transfer queue),
        update their status to 'DELETED' so:
        1. Unique episode constraints are freed.
        2. Channel ingest won't reject incoming shares as duplicate.
        3. Radar and scout will detect the missing episodes and re-scout / re-download them.
        Also synchronizes watchlist.db collected_episodes.
        \"\"\"
        import asyncpg
        try:
            phys_inv = await cls.get_physical_inventory()
            if not phys_inv:
                logger.info("Physical inventory is empty; skipping physical reconciliation to prevent accidental mass purge.")
                return {"success": True, "purged_count": 0, "purged_items": []}

            conn = await asyncpg.connect(PG_DSN)
            
            active_jobs = await conn.fetch(\"\"\"
                SELECT DISTINCT j.title, j.season 
                FROM transfer_queue_tasks q 
                JOIN channel_ingest_jobs j ON j.id = q.reference_id
                WHERE q.status IN ('PENDING', 'RUNNING') AND j.title IS NOT NULL
            \"\"\")
            active_shows = {
                (re.sub(r'[^\\w\\u4e00-\\u9fa5]', '', str(r['title'] or '')), int(r['season'] or 1))
                for r in active_jobs
            }

            rows = await conn.fetch(\"\"\"
                SELECT r.id, t.id as task_id, t.title, COALESCE(r.season, t.season, 1) as season, r.episode, r.created_at
                FROM resources r
                JOIN tasks t ON t.id = r.task_id
                WHERE r.status = 'ACCEPTED' 
                  AND t.media_type != 'MOVIE' 
                  AND r.episode IS NOT NULL
                  AND r.created_at < (NOW() - ($1 || ' minutes')::interval)
                ORDER BY t.title, r.episode
            \"\"\", str(max_age_minutes))

            purged_ids = []
            purged_items = []
            reconciled_shows = set()
            for r in rows:
                raw_title = str(r['title'] or '').strip()
                clean_t = re.sub(r'[^\\w\\u4e00-\\u9fa5]', '', raw_title)
                s = int(r['season'] or 1)
                ep = int(r['episode'])
                key = (clean_t, s)
                if key in active_shows:
                    continue
                if key in phys_inv:
                    # Show folder exists on physical disk!
                    if ep not in phys_inv[key]:
                        # File was deleted from physical disk!
                        purged_ids.append(r['id'])
                        purged_items.append({
                            "id": r['id'],
                            "title": raw_title,
                            "season": s,
                            "episode": ep
                        })
                        reconciled_shows.add((raw_title, clean_t, s))

            if purged_ids:
                await conn.execute(\"\"\"
                    UPDATE resources
                    SET status = 'DELETED',
                        reject_reason = '物理网盘文件已删除，实盘核销释放重新转存'
                    WHERE id = ANY($1::int[])
                \"\"\", purged_ids)
                logger.info("Reconciled and purged %d deleted resource records from PG media_bot_db: %s", len(purged_ids), [(it['title'], it['season'], it['episode']) for it in purged_items[:10]])

            await conn.close()

            if reconciled_shows:
                def _sync_watchlist_sync():
                    conn_w = _open_local_db()
                    cw = conn_w.cursor()
                    for title, clean_t, s in reconciled_shows:
                        current_real_eps = sorted(list(phys_inv.get((clean_t, s), set())))
                        cw.execute(\"\"\"
                            UPDATE watchlist
                            SET collected_episodes = ?
                            WHERE (title = ? OR title LIKE ?) AND season = ?
                        \"\"\", (json.dumps(current_real_eps), title, f"%{clean_t}%", s))
                    conn_w.commit()
                    conn_w.close()
                await asyncio.to_thread(_sync_watchlist_sync)

            return {
                "success": True,
                "purged_count": len(purged_ids),
                "purged_items": purged_items,
            }
        except Exception as e:
            logger.exception("Error in reconcile_physical_inventory: %s", e)
            return {"success": False, "error": str(e), "purged_count": 0}

    @classmethod
    async def get_physical_inventory(cls) -> Dict[Tuple[str, int], Set[int]]:"""

assert old_anchor in text, "old_anchor not found in cloud_inventory_service.py"
text = text.replace(old_anchor, reconcile_func)

with open("/home/ubuntu/如昔项目归总/影视追新机器人/services/cloud_inventory_service.py", "w", encoding="utf-8") as f:
    f.write(text)

print("cloud_inventory_service.py patched successfully!")
