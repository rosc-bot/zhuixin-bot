import asyncio
import json
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import aiohttp
from yarl import URL

from config import (
    BASE_DIR,
    FRAMEHDR_BASE_URL,
    FRAMEHDR_COOKIE_FILE,
    FRAMEHDR_ENABLED,
    FRAMEHDR_PASSWORD,
    FRAMEHDR_USERNAME,
)

logger = logging.getLogger(__name__)


class FrameHdrService:
    _session: Optional[aiohttp.ClientSession] = None
    _lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    async def get_session(cls) -> aiohttp.ClientSession:
        async with cls._lock:
            if cls._session is None or cls._session.closed:
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                }
                cookie_jar = aiohttp.CookieJar(unsafe=True)
                cls._session = aiohttp.ClientSession(headers=headers, cookie_jar=cookie_jar)
                cls._load_cookies()
            return cls._session

    @classmethod
    def _load_cookies(cls):
        cookie_path = Path(FRAMEHDR_COOKIE_FILE)
        if cookie_path.exists():
            try:
                with open(cookie_path, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                url_obj = URL(FRAMEHDR_BASE_URL)
                for c in cookies:
                    cls._session.cookie_jar.update_cookies({c["name"]: c["value"]}, url_obj)
                logger.info("Loaded %d cookies for FrameHdr from %s", len(cookies), cookie_path)
            except Exception as e:
                logger.warning("Failed to load FrameHdr cookies: %s", e)

    @classmethod
    def _save_cookies(cls):
        cookie_path = Path(FRAMEHDR_COOKIE_FILE)
        try:
            cookie_path.parent.mkdir(parents=True, exist_ok=True)
            cookies_to_save = []
            for cookie in cls._session.cookie_jar:
                cookies_to_save.append({
                    "name": cookie.key,
                    "value": cookie.value,
                    "domain": cookie["domain"],
                    "path": cookie["path"],
                })
            with open(cookie_path, "w", encoding="utf-8") as f:
                json.dump(cookies_to_save, f, ensure_ascii=False, indent=2)
            logger.info("Saved %d cookies for FrameHdr to %s", len(cookies_to_save), cookie_path)
        except Exception as e:
            logger.warning("Failed to save FrameHdr cookies: %s", e)

    @classmethod
    async def login(cls) -> bool:
        if not FRAMEHDR_ENABLED or not FRAMEHDR_USERNAME or not FRAMEHDR_PASSWORD:
            logger.warning("FrameHdr is disabled or credentials not set.")
            return False

        session = await cls.get_session()
        login_url = f"{FRAMEHDR_BASE_URL.rstrip('/')}/login.php"
        logger.info("Executing FrameHdr login for user: %s", FRAMEHDR_USERNAME)

        try:
            async with session.get(login_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                html = await resp.text()

            form_match = re.search(r"<form[^>]*id=[\x27\x22]loginForm[\x27\x22][^>]*>(.*?)</form>", html, re.S)
            data: Dict[str, str] = {
                "username": FRAMEHDR_USERNAME,
                "password": FRAMEHDR_PASSWORD,
                "login_mode": "password",
            }
            if form_match:
                for inp in re.finditer(r"<input[^>]*>", form_match.group(1)):
                    tag = inp.group(0)
                    n = re.search(r"name=[\x27\x22]([^\x27\x22]+)[\x27\x22]", tag)
                    v = re.search(r"value=[\x27\x22]([^\x27\x22]*)[\x27\x22]", tag)
                    if n and n.group(1) not in data:
                        data[n.group(1)] = v.group(1) if v else ""

            post_headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": login_url,
            }
            async with session.post(
                login_url,
                data=data,
                headers=post_headers,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as post_resp:
                await post_resp.text()

            has_session = any(c.key == "wangpan_session" for c in session.cookie_jar)
            if has_session:
                logger.info("FrameHdr login successful! Session cookie obtained.")
                cls._save_cookies()
                return True
            else:
                logger.error("FrameHdr login failed: wangpan_session not found in cookies.")
                return False
        except Exception as exc:
            logger.exception("FrameHdr login encountered error: %s", exc)
            return False

    @classmethod
    async def ensure_logged_in(cls) -> bool:
        session = await cls.get_session()
        has_session = any(c.key == "wangpan_session" for c in session.cookie_jar)
        if not has_session:
            return await cls.login()
        return True

    @classmethod
    def extract_episodes_from_text(cls, text: str) -> Set[int]:
        eps: Set[int] = set()
        if not text:
            return eps

        # 1. "更新至第X集" / "更至X集" / "更新到X集" / "全X集" -> 1..X
        for m in re.finditer(r"(?:更新至|更至|更新到|更新|全)\s*(?:第)?\s*0*(\d{1,4})\s*(?:集|话)?", text):
            val = int(m.group(1))
            if 1 <= val <= 2500 and val not in (1080, 2160, 720, 2023, 2024, 2025, 2026):
                eps.update(range(1, val + 1))

        # 2. Ranges: E01-E04, EP01-EP10, 01-04集
        for m in re.finditer(r"(?:[Ee]|EP|ep)?\s*0*(\d{1,4})\s*(?:-|~|到|至)\s*(?:[Ee]|EP|ep)?\s*0*(\d{1,4})\s*(?:集|话)?", text):
            s_ep, e_ep = int(m.group(1)), int(m.group(2))
            if 1 <= s_ep <= e_ep <= 2500 and (e_ep - s_ep) <= 150:
                eps.update(range(s_ep, e_ep + 1))

        # 3. Single tokens: E03, EP03, 第03集
        for m in re.finditer(r"(?:[Ee]|EP|ep)\s*0*(\d{1,4})\b", text):
            val = int(m.group(1))
            if 1 <= val <= 2500 and val not in (1080, 2160, 720, 2023, 2024, 2025, 2026):
                eps.add(val)
        for m in re.finditer(r"第\s*0*(\d{1,4})\s*(?:集|话)", text):
            val = int(m.group(1))
            if 1 <= val <= 2500 and val not in (1080, 2160, 720, 2023, 2024, 2025, 2026):
                eps.add(val)

        return eps

    @classmethod
    async def search_series(
        cls,
        title: str,
        season: int = 1,
        episodes: Optional[List[int]] = None,
        tmdb_id: Optional[int] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        if not FRAMEHDR_ENABLED:
            return []

        clean_title = re.sub(r"[^\w\u4e00-\u9fa5]", "", title)
        if not clean_title:
            return []

        await cls.ensure_logged_in()
        session = await cls.get_session()

        import difflib
        aliases = []
        if tmdb_id:
            try:
                from config import TMDB_API_KEY
                if TMDB_API_KEY:
                    a_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/alternative_titles?api_key={TMDB_API_KEY}"
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as _s:
                        async with _s.get(a_url) as _r:
                            if _r.status == 200:
                                _d = await _r.json()
                                for _it in _d.get('results', []):
                                    _t = str(_it.get('title') or '').strip()
                                    if _it.get('iso_3166_1') in ('CN', 'TW', 'HK') and re.search(r'[一-龥]', _t):
                                        if _t not in aliases:
                                            aliases.append(_t)
            except Exception:
                pass

        queries = [title] + [a for a in aliases if a != title]
        target_cleans = [clean_title] + [
            re.sub(r'[^\w一-龥]', '', a) for a in aliases
            if re.sub(r'[^\w一-龥]', '', a)
        ]

        def title_matches_card(c_title: str) -> bool:
            c_clean = re.sub(r'[^\w一-龥]', '', c_title)
            for tc in target_cleans:
                if tc in c_clean or c_clean in tc:
                    return True
                if len(tc) >= 4 and difflib.SequenceMatcher(None, tc, c_clean[:len(tc)+5]).ratio() >= 0.75:
                    return True
            return False

        season_chinese = {
            1: ['第一季', '第1季'],
            2: ['第二季', '第2季'],
            3: ['第三季', '第3季'],
            4: ['第四季', '第4季'],
            5: ['第五季', '第5季'],
        }
        target_s_names = season_chinese.get(season, [f'第{season}季'])
        s_tokens = target_s_names + [f'S{season:02d}', f'S{season}', f'第{season}期']

        matched_cards = []
        for q in queries:
            search_url = f"{FRAMEHDR_BASE_URL.rstrip('/')}/search.php?q={urllib.parse.quote(q)}"
            try:
                async with session.get(search_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    html = await resp.text()
            except Exception as e:
                logger.warning("FrameHdr search request failed for '%s': %s", q, e)
                continue

            card_matches = re.finditer(
                r"detail\.php\?id=(\d+).*?<h3[^>]*class=[^>]*card-title[^>]*>([^<]+)</h3>",
                html,
                re.S,
            )
            cards = [{'id': m.group(1), 'title': m.group(2).strip()} for m in card_matches]
            for c in cards:
                if not title_matches_card(c['title']):
                    continue
                if season == 1:
                    later_tokens = [
                        '第二季', '第2季', '第三季', '第3季',
                        '第四季', '第4季', '第五季', '第5季',
                        'S02', 'S03', 'S04', 'S05',
                        'S2', 'S3', 'S4', 'S5',
                    ]
                    if any(k in c['title'] for k in later_tokens):
                        continue
                    matched_cards.append(c)
                else:
                    if any(tok in c['title'] for tok in s_tokens):
                        matched_cards.append(c)
            if matched_cards:
                break

        if not matched_cards:
            logger.info("FrameHdr found no matched cards for '%s' Season %d across %d queries", title, season, len(queries))
            return []

        target_set = set(episodes or [])
        results: List[Dict[str, Any]] = []

        for mc in matched_cards[:2]:
            detail_url = f"{FRAMEHDR_BASE_URL.rstrip('/')}/detail.php?id={mc['id']}"
            try:
                async with session.get(detail_url, timeout=aiohttp.ClientTimeout(total=15)) as d_resp:
                    d_html = await d_resp.text()

                if "_isLoggedIn = false" in d_html:
                    logger.info("FrameHdr detail page detected logged-out state. Re-authenticating...")
                    await cls.login()
                    async with session.get(detail_url, timeout=aiohttp.ClientTimeout(total=15)) as retry_resp:
                        d_html = await retry_resp.text()

                links = re.findall(
                    r"copyToClipboard\(\x27([^\x27]+)\x27\s*,\s*\x27([^\x27]*)\x27\s*,\s*(\d+)\s*,\s*\x27([^\x27]*)\x27\)",
                    d_html,
                )
                descriptions = re.findall(
                    r"<div class=[\x27\x22]link-description[\x27\x22]>([^<]*)</div>",
                    d_html,
                )
                publish_times = re.findall(
                    r"<span class=[\x27\x22]link-publish-time[\x27\x22]>发布时间：([^<]+)</span>",
                    d_html,
                )
                publishers = re.findall(
                    r"<span class=[\x27\x22]link-publisher-name[\x27\x22]>([^<]+)</span>",
                    d_html,
                )

                for idx, (url, raw_code, link_id, disk_name) in enumerate(links):
                    provider = (
                        "guangya"
                        if ("光鸭" in disk_name or "guangyapan" in url)
                        else ("115" if "115" in disk_name else "unknown")
                    )

                    clean_code = str(raw_code or "").strip(" -")
                    full_url = url
                    if clean_code and len(clean_code) >= 4 and "guangyapan" in url and "code=" not in url:
                        full_url = f"{url}?code={clean_code}"

                    desc = descriptions[idx].strip() if idx < len(descriptions) else ""
                    pub_time = publish_times[idx].strip() if idx < len(publish_times) else ""
                    pub_name = publishers[idx].strip() if idx < len(publishers) else ""

                    extracted_eps = cls.extract_episodes_from_text(f"{mc['title']} {desc}")
                    matched_eps = sorted(target_set & extracted_eps) if target_set else sorted(extracted_eps)

                    results.append({
                        "msg_id": 800000000 + int(link_id),
                        "chat_title": f"帧影·{pub_name or '分享'}",
                        "title": title,
                        "season": season,
                        "provider": provider,
                        "url": full_url,
                        "matched_episodes": matched_eps,
                        "date_cst": pub_time,
                        "snippet": f"[帧影分享] {mc['title']} | {desc}",
                        "source": "framehdr",
                        "tmdb_id": tmdb_id,
                    })
                    if len(results) >= limit:
                        break
            except Exception as d_err:
                logger.warning("Failed to fetch detail for FrameHdr ID %s: %s", mc["id"], d_err)

        return results[:limit]

    @classmethod
    async def get_today_updates(cls, limit: int = 25) -> List[Dict[str, Any]]:
        if not FRAMEHDR_ENABLED:
            return []

        await cls.ensure_logged_in()
        session = await cls.get_session()
        updates_url = f"{FRAMEHDR_BASE_URL.rstrip('/')}/updates.php"

        try:
            async with session.get(updates_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                html = await resp.text()

            rows = re.findall(r"<tr[^>]*>.*?</tr>", html, re.S)
            items = []
            for r in rows:
                link_m = re.search(r"href=[\x27\x22]detail\.php\?id=(\d+)[\x27\x22][^>]*>(.*?)</a>", r, re.S)
                if not link_m:
                    continue
                did = link_m.group(1)
                t = re.sub(r"<[^>]+>", "", link_m.group(2)).strip()
                tds = re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)
                clean_tds = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", td)).strip() for td in tds]

                items.append({
                    "id": did,
                    "title": t,
                    "columns": clean_tds,
                })
                if len(items) >= limit:
                    break
            return items
        except Exception as e:
            logger.warning("FrameHdr updates request failed: %s", e)
            return []

    @classmethod
    async def close(cls):
        async with cls._lock:
            if cls._session and not cls._session.closed:
                await cls._session.close()
                cls._session = None

    @classmethod
    async def checkin(cls) -> Dict[str, Any]:
        """每日自动向 FrameHdr 发起签到并获取积分"""
        if not FRAMEHDR_ENABLED:
            return {"success": False, "message": "FrameHdr 未启用"}
        
        ok = await cls.ensure_logged_in()
        if not ok:
            return {"success": False, "message": "FrameHdr 登录失败，无法签到"}
            
        session = await cls.get_session()
        try:
            from datetime import datetime
            now = datetime.now()
            init_url = f"{FRAMEHDR_BASE_URL}/api/checkin.php?action=init&year={now.year}&month={now.month}"
            async with session.get(init_url) as resp:
                if resp.status != 200:
                    return {"success": False, "message": f"获取签到状态失败 HTTP {resp.status}"}
                data = await resp.json()
                csrf_token = data.get("data", {}).get("csrf_token")
                has_checked = data.get("data", {}).get("has_checked_today")
                if has_checked:
                    logger.info("FrameHdr 今日已签到，无需重复签到")
                    return {"success": True, "message": "今日已签到", "data": data.get("data")}
                    
            if not csrf_token:
                return {"success": False, "message": "未能获取签到 CSRF Token"}
                
            form = aiohttp.FormData()
            form.add_field("csrf_token", csrf_token)
            headers = {
                "Referer": f"{FRAMEHDR_BASE_URL}/",
                "Origin": FRAMEHDR_BASE_URL,
                "X-Requested-With": "XMLHttpRequest",
            }
            post_url = f"{FRAMEHDR_BASE_URL}/api/checkin.php?action=checkin"
            async with session.post(post_url, data=form, headers=headers) as post_resp:
                if post_resp.status == 200:
                    res_json = await post_resp.json()
                    logger.info("FrameHdr 签到结果: %s", res_json)
                    return res_json
                else:
                    return {"success": False, "message": f"签到请求失败 HTTP {post_resp.status}"}
        except Exception as e:
            logger.exception("FrameHdr checkin error: %s", e)
            return {"success": False, "message": str(e)}
