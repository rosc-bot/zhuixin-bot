import asyncio
from services.guangya_probe import _share_id_and_code, _post_json

async def test():
    u1 = "https://www.guangyapan.com/s/1942450061206507590_ad5KFI8EdLN_2Dyr"
    u2 = "https://www.guangyapan.com/s/1929778163961843808_ad5KFI8EdLN_2Dyr"
    
    import aiohttp
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://www.guangyapan.com",
        "Referer": "https://www.guangyapan.com/",
        "User-Agent": "Mozilla/5.0",
    }
    async with aiohttp.ClientSession() as session:
        for name, u in [("u1 (灵境行者)", u1), ("u2 (逆天邪神)", u2)]:
            sid, code = _share_id_and_code(u)
            token_res = await _post_json(session, "https://api.guangyapan.com/userres/v1/get_share_access_token", {"shareId": sid, "code": code}, headers)
            acc = token_res["data"]["accessToken"]
            auth_h = {**headers, "authorization": f"Bearer {acc}"}
            files_res = await _post_json(session, "https://api.guangyapan.com/userres/v1/get_share_page_files_list", {"shareId": sid, "page": 1, "pageSize": 5, "accessToken": acc}, auth_h)
            items = files_res["data"]["list"]
            print(f"=== {name} ===")
            for item in items:
                fn = item.get("fileName")
                rt = item.get("resType")
                print(f"  • {fn} (type={rt})")

if __name__ == "__main__":
    asyncio.run(test())
