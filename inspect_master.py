import asyncio, aiohttp, json

async def inspect_master():
    import asyncpg
    conn = await asyncpg.connect("postgresql://mediabot:mediabot_secret_pass@127.0.0.1:5432/media_bot_db")
    row = await conn.fetchrow("SELECT auth_token, target_folder_id FROM cloud_configs WHERE name = 'guangya'")
    await conn.close()
    
    auth_data = json.loads(row["auth_token"])
    ref_token = auth_data.get("refresh_token")
    master_id = row["target_folder_id"] or "1942305989699285071"
    
    async with aiohttp.ClientSession() as session:
        t_resp = await session.post(
            "https://account.guangyapan.com/v1/auth/token",
            json={"client_id": "aMe-8VSlkrbQXpUR", "grant_type": "refresh_token", "refresh_token": ref_token},
            headers={"Content-Type": "application/json"}
        )
        t_data = await t_resp.json()
        acc = t_data.get("access_token")
        
        headers = {"Authorization": f"Bearer {acc}", "Content-Type": "application/json"}
        
        async def get_list(pid):
            r = await session.post(
                "https://api.guangyapan.com/userres/v1/file/get_file_list",
                json={"parentId": str(pid), "pageNum": 1, "pageSize": 300},
                headers=headers
            )
            res = await r.json()
            return res.get("data", {}).get("list", [])
            
        print(f"Listing master_id: {master_id}")
        l1 = await get_list(master_id)
        for it1 in l1:
            name1 = it1.get("fileName")
            fid1 = it1.get("fileId")
            rtype1 = it1.get("resType")
            print(f"📁 L1: {name1} (id={fid1}, type={rtype1})")
            l2 = await get_list(fid1)
            for it2 in l2:
                name2 = it2.get("fileName")
                fid2 = it2.get("fileId")
                rtype2 = it2.get("resType")
                print(f"   📁 L2: {name2} (id={fid2}, type={rtype2})")
                l3 = await get_list(fid2)
                print(f"      -> {len(l3)} items in {name2}")

if __name__ == "__main__":
    asyncio.run(inspect_master())
