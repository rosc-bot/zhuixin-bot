import asyncio, aiohttp, json

async def check_root():
    import asyncpg
    conn = await asyncpg.connect("postgresql://mediabot:mediabot_secret_pass@127.0.0.1:5432/media_bot_db")
    row = await conn.fetchrow("SELECT auth_token, target_folder_id FROM cloud_configs WHERE name = 'guangya'")
    await conn.close()
    
    auth_data = json.loads(row["auth_token"])
    ref_token = auth_data.get("refresh_token")
    target_fid = row["target_folder_id"]
    print("Target folder configured in DB:", target_fid)
    
    async with aiohttp.ClientSession() as session:
        t_resp = await session.post(
            "https://account.guangyapan.com/v1/auth/token",
            json={"client_id": "aMe-8VSlkrbQXpUR", "grant_type": "refresh_token", "refresh_token": ref_token},
            headers={"Content-Type": "application/json"}
        )
        t_data = await t_resp.json()
        acc = t_data.get("access_token")
        
        headers = {"Authorization": f"Bearer {acc}", "Content-Type": "application/json"}
        r = await session.post(
            "https://api.guangyapan.com/userres/v1/file/get_file_list",
            json={"parentId": "0", "pageNum": 1, "pageSize": 100},
            headers=headers
        )
        res = await r.json()
        print("Root folders on Guangya:")
        for item in res.get("data", {}).get("list", []):
            name = item.get("fileName")
            fid = item.get("fileId")
            rtype = item.get("resType")
            print(f"  • {name} (id={fid}, type={rtype})")

if __name__ == "__main__":
    asyncio.run(check_root())
