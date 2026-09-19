import os
import html
import asyncio
import logging
import hashlib
import re
import time
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from config import BOT_TOKEN, ADMIN_TG_ID
from database import DatabaseService
from services.calendar_service import CalendarService
from services.scout_service import ScoutService
from services.library_service import LibraryService

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(name)s: %(message)s")
logger = logging.getLogger("ZhuiXinBot")

# In-memory short callback cache to keep callback_data <= 64 bytes
CB_PAYLOAD_CACHE: Dict[str, Dict[str, Any]] = {}

def register_cb_payload(prefix: str, data: Dict[str, Any]) -> str:
    serialized = json_safe_key(data)
    h = hashlib.md5(serialized.encode("utf-8")).hexdigest()[:8]
    CB_PAYLOAD_CACHE[h] = data
    return f"{prefix}:{h}"

def get_cb_payload(key: str) -> Optional[Dict[str, Any]]:
    return CB_PAYLOAD_CACHE.get(key)

def json_safe_key(d: Dict[str, Any]) -> str:
    import json
    return json.dumps(d, sort_keys=True, ensure_ascii=True)

def escape(text: Any) -> str:
    return html.escape(str(text or ""))

class SearchFSM(StatesGroup):
    waiting_for_query = State()

dp = Dispatcher()


# ==================== 1. 主菜单与分类导航 ====================

def get_main_menu_markup():
    text = (
        "👋 <b>爸爸好！欢迎使用【影视追新机器人】！</b> ✨\n\n"
        "小助手专为您的私人影视库打造，提供每日追剧排期、缺集智能诊断、锁定影视群聊/频道资源打捞以及网盘全自动转存！\n\n"
        "💡 <b>核心功能指南：</b>\n"
        "• <b>📅 追剧日历：</b>数字影视全量 15 大分类每日排期，支持一键追更\n"
        "• <b>📡 缺集雷达：</b>严谨对齐 TMDB 分季结构，智能识别缺集、断档与追新\n"
        "• <b>📺 追更清单：</b>管理在追影视，支持【仅追最新】与【全量补齐】\n"
        "• <b>🔄 自动转存：</b>缺集命中后自动送入转存队列，Emby 规范化扫库！"
    )
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="📅 追剧日历 (分类导航)", callback_data="calendar_menu"),
        types.InlineKeyboardButton(text="📡 缺集与追新雷达", callback_data="menu:radar:LATEST"),
    )
    builder.row(
        types.InlineKeyboardButton(text="📺 我的追更清单", callback_data="menu:my_follow"),
        types.InlineKeyboardButton(text="🔥 热播自动追新", callback_data="menu:auto_ingest"),
    )
    builder.row(
        types.InlineKeyboardButton(text="🔍 搜索剧名加追", callback_data="menu:search_prompt"),
        types.InlineKeyboardButton(text="🔍 扫描网盘物理文件", callback_data="menu:scan_cloud"),
    )
    builder.row(
        types.InlineKeyboardButton(text="⚡ 实时转存队列", callback_data="menu:queue_status"),
        types.InlineKeyboardButton(text="🔄 全库打捞资源", callback_data="menu:sync_now"),
    )
    builder.row(
        types.InlineKeyboardButton(text="⚙️ 追更管理", callback_data="menu:manage"),
    )
    return text, builder.as_markup()



@dp.message(Command("hot"))
async def cmd_hot_auto_ingest(message: types.Message):
    """Direct command /hot to open hot broadcast auto-ingest menu."""
    await show_auto_ingest_menu(message)


@dp.message(Command("start"))
@dp.message(Command("help"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    text, markup = get_main_menu_markup()
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@dp.callback_query(F.data == "menu:overview")
async def cb_overview(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    text, markup = get_main_menu_markup()
    try:
        await call.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest as exc:
        logger.warning("Telegram overview edit failed: %s", exc)


@dp.message(Command("calendar"))
@dp.callback_query(F.data == "calendar_menu")
async def show_calendar_categories(event: types.Message | types.CallbackQuery):
    if isinstance(event, types.CallbackQuery):
        await event.answer()

    text = (
        "📅 <b>追剧日历 · 全部分类导航</b>\n\n"
        "请选择您想要查看的剧集分类（数据实时同步自数字影视每日排期）："
    )
    builder = InlineKeyboardBuilder()
    for key, cfg in CalendarService.CATEGORIES.items():
        builder.button(text=cfg["name"], callback_data=f"cal_view:{key}:0:1")
    builder.adjust(2)
    builder.row(
        types.InlineKeyboardButton(text="📡 查看缺集雷达", callback_data="menu:radar:LATEST"),
        types.InlineKeyboardButton(text="🔙 返回主菜单", callback_data="menu:overview")
    )
    if isinstance(event, types.CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except TelegramBadRequest as exc:
            logger.warning("Telegram message edit failed: %s", exc)
    else:
        await event.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("cal_view:"))
async def cb_calendar_view(call: types.CallbackQuery):
    await call.answer()
    parts = call.data.split(":")
    cat_key = parts[1]
    day_offset = int(parts[2]) if len(parts) > 2 else 0
    page = int(parts[3]) if len(parts) > 3 else 1

    cal_data = await CalendarService.get_category_schedule(cat_key, day_offset=day_offset)
    cat_name = cal_data.get("cat_name") or "追剧日历"
    date_display = cal_data.get("day_text") or "暂无更新数据"
    all_shows = cal_data.get("shows", [])

    library = await LibraryService.get_transferred_library()
    user_subs = await DatabaseService.list_subscriptions(call.from_user.id)
    following_map = {re.sub(r'[^\w\u4e00-\u9fa5]', '', s["title"]): s for s in user_subs}

    page_size = 5
    total_shows = len(all_shows)
    total_pages = max(1, (total_shows + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * page_size
    page_shows = all_shows[start_idx:start_idx + page_size]

    text = (
        f"📅 <b>{cat_name} · 排期日历</b>\n"
        f"🗓️ <b>日期：</b><code>{date_display}</code>  (共 {total_shows} 部影视)\n"
        f"──────────────────────\n"
    )

    builder = InlineKeyboardBuilder()

    if not page_shows:
        text += "<i>该分类在当前选定日期暂无播出排期安排。</i>\n"
    else:
        for idx, show in enumerate(page_shows, start=start_idx + 1):
            title = show["title"]
            sea = show.get("season", 1)
            ep_disp = show.get("ep_display", "")
            eps = show.get("episodes") or []
            is_prem = show.get("is_premiere", False)
            prem_tag = " [🌟首播]" if is_prem else ""

            clean_t = re.sub(r'[^\w\u4e00-\u9fa5]', '', title)
            is_followed = clean_t in following_map
            match_entry = LibraryService.match_show_in_library(library, title, sea)
            if not eps:
                eval_res = {
                    "badge": "⚪ [集数待确认]",
                    "action_type": "none",
                }
            else:
                eval_res = await LibraryService.evaluate_item_status(
                    match_entry, eps, is_following=is_followed, follow_mode="LATEST"
                )

            badge = eval_res["badge"]
            action_type = eval_res["action_type"]

            text += (
                f"{idx}. <b>《{escape(title)}》</b> 第 {sea} 季 <code>{escape(ep_disp)}</code>{prem_tag}\n"
                f"   └ 媒体库状态：<b>{badge}</b>\n"
            )

            if action_type == "scout":
                cb_key = register_cb_payload("sq", {"title": title, "season": sea, "cat": cat_key, "offset": day_offset, "page": page})
                builder.row(types.InlineKeyboardButton(text=f"🔥 打捞《{title[:8]}》缺集", callback_data=cb_key))
            elif action_type == "completed":
                cb_key = register_cb_payload("sq", {"title": title, "season": sea, "cat": cat_key, "offset": day_offset, "page": page})
                builder.row(types.InlineKeyboardButton(text=f"✅ 已入库 (点此可重打捞)", callback_data=cb_key))
            elif action_type == "none":
                pass
            else:
                cb_key = register_cb_payload("fq", {"title": title, "season": sea, "poster": show.get("poster"), "cat": cat_key, "offset": day_offset, "page": page})
                builder.row(types.InlineKeyboardButton(text=f"➕ 追更《{title[:8]}》S{sea}", callback_data=cb_key))

    text += f"\n📄 第 <code>{page}/{total_pages}</code> 页"

    # Pagination row
    nav_row = []
    if page > 1:
        nav_row.append(types.InlineKeyboardButton(text="◀ 上一页", callback_data=f"cal_view:{cat_key}:{day_offset}:{page-1}"))
    nav_row.append(types.InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data=f"cal_view:{cat_key}:{day_offset}:{page}"))
    if page < total_pages:
        nav_row.append(types.InlineKeyboardButton(text="下一页 ▶", callback_data=f"cal_view:{cat_key}:{day_offset}:{page+1}"))
    if nav_row:
        builder.row(*nav_row)

    # Date navigation
    if cat_key in ("domestic", "western", "jp-kr", "movie", "reality", "documentary", "talkshow", "anime"):
        builder.row(
            types.InlineKeyboardButton(text="◀ 前一天", callback_data=f"cal_view:{cat_key}:{day_offset-1}:1"),
            types.InlineKeyboardButton(text="📅 回今天", callback_data=f"cal_view:{cat_key}:0:1"),
            types.InlineKeyboardButton(text="后一天 ▶", callback_data=f"cal_view:{cat_key}:{day_offset+1}:1"),
        )

    builder.row(
        types.InlineKeyboardButton(text="📂 切换分类", callback_data="calendar_menu"),
        types.InlineKeyboardButton(text="📡 缺集雷达", callback_data="menu:radar:LATEST"),
        types.InlineKeyboardButton(text="🔙 返回主菜单", callback_data="menu:overview"),
    )

    if isinstance(call, types.CallbackQuery):
        try:
            await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception as exc:
            logger.exception("Telegram menu edit failed: %s", exc)
            await call.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await call.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


# ==================== 2. 缺集与追新雷达 (TMDB 分季对齐 + 主动忽略/关闭) ====================

@dp.callback_query(F.data.startswith("menu:radar"))
@dp.message(Command("radar"))
async def show_radar_dashboard(
    event: types.Message | types.CallbackQuery,
    answer_callback: bool = True,
):
    if isinstance(event, types.CallbackQuery):
        if answer_callback:
            await event.answer()
        parts = event.data.split(":")
        follow_mode = parts[2] if len(parts) >= 3 else "LATEST"
        try:
            await event.message.edit_text("⏳ <b>正在刷新缺集雷达，请稍候…</b>", parse_mode="HTML")
        except TelegramBadRequest:
            pass
    else:
        follow_mode = "LATEST"

    data = await CalendarService.get_today_shows("domestic")
    today_shows = data.get("shows", [])
    radar = await LibraryService.get_radar_summary(today_shows, follow_mode=follow_mode)

    alt_mode = "FULL" if follow_mode == "LATEST" else "LATEST"
    alt_label = "🔄 切换为【全量补齐】模式" if follow_mode == "LATEST" else "⚡ 切换为【仅追最新】模式"
    cur_label = "⚡ 仅追最新 (现有集数续更)" if follow_mode == "LATEST" else "🔄 全量补齐 (包含前期旧集)"

    text = (
        "📡 <b>缺集与追新智能雷达</b>\n\n"
        f"⚙️ <b>当前雷达模式：</b><code>{cur_label}</code>\n"
        f"已对齐 TMDB 官方分季元数据，诊断媒体库 <b>{radar['total_library_tasks']}</b> 部影视：\n"
        "──────────────────────\n"
    )

    builder = InlineKeyboardBuilder()

    # Section 1: Today Airing Matches
    airing_matches = radar["airing_today_matches"]
    visible_airing = airing_matches[:8]
    if airing_matches:
        text += f"🔥 <b>今日有排期的已入库剧集 ({len(airing_matches)} 部)：</b>\n"
        for m in visible_airing:
            t = escape(m["title"])
            sea = m["season"]
            disp = escape(m["ep_display"])
            badge = m["eval"]["badge"]
            text += f" • <b>《{t}》</b> 第 {sea} 季 <code>{disp}</code>\n   └ 状态：<b>{badge}</b>\n"
            if m["eval"]["action_type"] == "scout":
                m_eps = m["eval"].get("missing_episodes") or m.get("today_episodes") or []
                cb_key = register_cb_payload("sq", {"title": m["title"], "season": sea, "target_eps": m_eps, "act": "scout"})
                builder.row(types.InlineKeyboardButton(text=f"🔥 立即打捞《{t[:8]}》今日缺集", callback_data=cb_key))
        if len(airing_matches) > len(visible_airing):
            text += f"   └ 其余 {len(airing_matches) - len(visible_airing)} 部请使用日历或下一轮雷达查看。\n"
        text += "\n"
    else:
        text += "🔥 <b>今日排期：</b> 今日暂无已收录剧集的更新。\n\n"

    # Section 2: Missing Episodes with Rigorous Breakdown
    missing_list = radar["missing_in_library"]
    visible_missing = missing_list[:8]
    if missing_list:
        text += f"⚠️ <b>媒体库缺集与分季诊断 ({len(missing_list)} 部待补/追新)：</b>\n"
        for item in visible_missing:
            t = escape(item["title"])
            sea = item["season"]
            coll_cnt = len(item["episodes"])
            missing = item["missing_episodes"]
            diag = escape(str(item.get("reason_text") or "缺集待补")[:100])

            if missing:
                sorted_m = sorted(missing)
                if len(sorted_m) <= 6:
                    miss_preview = ", ".join(f"E{e:02d}" for e in sorted_m)
                elif sorted_m == list(range(min(sorted_m), max(sorted_m) + 1)):
                    miss_preview = f"E{min(sorted_m):02d} ~ E{max(sorted_m):02d} (共{len(sorted_m)}集)"
                else:
                    miss_preview = ", ".join(f"E{e:02d}" for e in sorted_m[:3]) + f"... ~ E{max(sorted_m):02d} (共{len(sorted_m)}集)"
            else:
                miss_preview = "无本季缺集 (已收齐/后续待播)"

            text += (
                f" • <b>《{t}》</b> 第 {sea} 季\n"
                f"   └ 📦 已存 {coll_cnt} 集 | 💡 <b>{diag}</b>\n"
                f"   └ 🔴 待收：<code>{miss_preview}</code>\n"
            )

            # Row 1: Scout button
            cb_key = register_cb_payload("sq", {"title": item["title"], "season": sea, "target_eps": missing, "act": "scout"})
            builder.row(types.InlineKeyboardButton(text=f"🔥 打捞《{item['title'][:8]}》S{sea} 缺集", callback_data=cb_key))

            # Row 2: Proactive Ignore/Close buttons!
            ign_buttons = []
            if len(missing) == 1:
                ep_val = missing[0]
                ign_s_key = register_cb_payload("ign", {"act": "single", "title": item["title"], "season": sea, "ep": ep_val, "mode": follow_mode})
                ign_buttons.append(types.InlineKeyboardButton(text=f"🚫 忽略 E{ep_val:02d}", callback_data=ign_s_key))
            elif len(missing) > 1:
                ign_m_key = register_cb_payload("ign", {"act": "menu", "title": item["title"], "season": sea, "missing": missing, "mode": follow_mode})
                ign_buttons.append(types.InlineKeyboardButton(text="🚫 缺集管理选项", callback_data=ign_m_key))

            ign_all_key = register_cb_payload("ign", {"act": "all", "title": item["title"], "season": sea, "mode": follow_mode})
            ign_buttons.append(types.InlineKeyboardButton(text="🛑 关闭全季提醒", callback_data=ign_all_key))

            if item.get("ignored_episodes"):
                unign_key = register_cb_payload("ign", {"act": "unignore", "title": item["title"], "season": sea, "mode": follow_mode})
                ign_buttons.append(types.InlineKeyboardButton(text="🔄 恢复监控", callback_data=unign_key))

            builder.row(*ign_buttons)
        if len(missing_list) > len(visible_missing):
            text += f"   └ 其余 {len(missing_list) - len(visible_missing)} 部缺集项目已折叠，使用全库打捞或切换模式处理。\n"
    else:
        text += "✅ <b>缺集诊断：</b> 太棒了！当前模式下所有连载剧集均无缺失集数！\n"

    text += f"\n📦 <b>完结收录：</b> 共有 <code>{len(radar['completed_in_library'])}</code> 部影视已全集/全片完整归档。"

    # Control buttons
    builder.row(
        types.InlineKeyboardButton(text=alt_label, callback_data=f"menu:radar:{alt_mode}")
    )
    builder.row(
        types.InlineKeyboardButton(text="📋 查看已关闭缺集名单", callback_data="menu:ignored_list"),
        types.InlineKeyboardButton(text="🔄 立即全库打捞", callback_data="menu:sync_now"),
    )
    builder.row(
        types.InlineKeyboardButton(text="📅 追剧日历", callback_data="calendar_menu"),
        types.InlineKeyboardButton(text="🔙 返回主菜单", callback_data="menu:overview"),
    )

    if isinstance(event, types.CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except TelegramBadRequest as exc:
            logger.warning("Telegram message edit failed: %s", exc)
    else:
        await event.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


# ==================== 3. 主动关闭/忽略缺集管理 (Proactive Ignore Handlers) ====================

@dp.callback_query(F.data.startswith("ign:"))
async def cb_ignore_handler(call: types.CallbackQuery):
    key = call.data[4:]
    payload = get_cb_payload(key)
    if not payload:
        await call.answer("⚠️ 该操作按钮已过期，请重新点击刷新。", show_alert=True)
        return

    act = payload.get("act")
    title = payload.get("title")
    season = int(payload.get("season") or 1)
    mode = payload.get("mode") or "LATEST"

    if act == "single":
        ep = int(payload.get("ep") or 0)
        await DatabaseService.add_ignored(title, season, ep)
        LibraryService.invalidate_radar_cache()
        if payload.get("from_menu"):
            await call.answer(f"✅ 已成功忽略第 {ep} 集！", show_alert=False)
            missing = [e for e in (payload.get("missing") or []) if e != ep]
            if not missing:
                event = call
                event.data = f"menu:radar:{mode}"
                await show_radar_dashboard(event, answer_callback=False)
                return
            payload["missing"] = missing
            payload["act"] = "menu"
            await render_ignore_menu(call, payload)
        else:
            await call.answer(f"✅ 已成功关闭《{title}》S{season} 第 {ep} 集的缺集提醒！", show_alert=True)
            event = call
            event.data = f"menu:radar:{mode}"
            await show_radar_dashboard(event, answer_callback=False)
    elif act == "ignore_page":
        eps = payload.get("eps") or []
        for ep in eps:
            await DatabaseService.add_ignored(title, season, ep)
        LibraryService.invalidate_radar_cache()
        await call.answer(f"✅ 已成功忽略本页 {len(eps)} 集！", show_alert=True)
        all_m = [e for e in (payload.get("missing") or []) if e not in eps]
        if not all_m:
            event = call
            event.data = f"menu:radar:{mode}"
            await show_radar_dashboard(event, answer_callback=False)
            return
        payload["missing"] = all_m
        payload["act"] = "menu"
        payload["page"] = max(0, int(payload.get("page") or 0) - 1)
        await render_ignore_menu(call, payload)
    elif act == "all":
        await DatabaseService.add_ignored(title, season, 0)
        LibraryService.invalidate_radar_cache()
        await call.answer(f"✅ 已成功关闭《{title}》S{season} 整季的所有缺集提醒！", show_alert=True)
        event = call
        event.data = f"menu:radar:{mode}"
        await show_radar_dashboard(event, answer_callback=False)
    elif act == "unignore" or act == "unignore_all":
        await DatabaseService.remove_ignored(title, season)
        LibraryService.invalidate_radar_cache()
        await call.answer(f"✅ 已恢复《{title}》S{season} 的缺集监控与提醒！", show_alert=True)
        if act == "unignore_all":
            list_page = max(0, int(payload.get("list_page") or 0))
            call.data = f"menu:ignored_list:{list_page}"
            await cb_ignored_list(call, answer_callback=False)
        else:
            event = call
            event.data = f"menu:radar:{mode}"
            await show_radar_dashboard(event, answer_callback=False)
    elif act == "clear_all":
        await DatabaseService.clear_all_ignored()
        LibraryService.invalidate_radar_cache()
        await call.answer("✅ 已清空所有忽略规则，全部恢复监控！", show_alert=True)
        list_page = max(0, int(payload.get("list_page") or 0))
        call.data = f"menu:ignored_list:{list_page}"
        await cb_ignored_list(call, answer_callback=False)
    elif act == "menu":
        await call.answer()
        await render_ignore_menu(call, payload)


async def render_ignore_menu(call: types.CallbackQuery, payload: Dict[str, Any]):
    title = payload.get("title")
    season = int(payload.get("season") or 1)
    mode = payload.get("mode") or "LATEST"
    missing = payload.get("missing") or []
    page = int(payload.get("page") or 0)
    page_size = 10
    total_pages = max(1, (len(missing) + page_size - 1) // page_size if missing else 1)
    page = max(0, min(page, total_pages - 1))
    
    cur_eps = missing[page * page_size : (page + 1) * page_size]
    
    sorted_all = sorted(missing)
    if len(sorted_all) <= 10:
        all_preview = ", ".join(f"E{e:02d}" for e in sorted_all)
    elif sorted_all == list(range(min(sorted_all), max(sorted_all) + 1)):
        all_preview = f"E{min(sorted_all):02d} ~ E{max(sorted_all):02d} (共{len(sorted_all)}集)"
    else:
        all_preview = ", ".join(f"E{e:02d}" for e in sorted_all[:5]) + f"... ~ E{max(sorted_all):02d} (共{len(sorted_all)}集)"

    text = (
        f"⚙️ <b>《{escape(title)}》第 {season} 季 · 缺集管理选项</b>\n\n"
        f"📌 <b>真实待补清单：</b><code>{all_preview}</code>\n"
        f"📄 <b>当前第 {page + 1}/{total_pages} 页：</b>\n\n"
        "💡 <i>如果您其他网盘里已有某些集数，可点击下方按钮选择忽略：</i>"
    )
    builder = InlineKeyboardBuilder()

    # Batch actions for this page if multiple
    if len(cur_eps) > 1:
        scout_p_k = register_cb_payload("sq", {"title": title, "season": season, "target_eps": cur_eps, "act": "scout"})
        ign_p_k = register_cb_payload("ign", {"act": "ignore_page", "title": title, "season": season, "eps": cur_eps, "missing": missing, "mode": mode, "page": page})
        builder.row(
            types.InlineKeyboardButton(text=f"🔥 打捞本页 ({len(cur_eps)}集)", callback_data=scout_p_k),
            types.InlineKeyboardButton(text=f"🚫 忽略本页 ({len(cur_eps)}集)", callback_data=ign_p_k),
        )

    # Individual buttons for current page
    for ep in cur_eps:
        k = register_cb_payload("ign", {"act": "single", "title": title, "season": season, "ep": ep, "mode": mode, "page": page, "missing": missing, "from_menu": True})
        builder.button(text=f"🚫 忽略 E{ep:02d}", callback_data=k)
    builder.adjust(2)

    # Pagination Controls
    nav_btns = []
    if page > 0:
        prev_k = register_cb_payload("ign", {"act": "menu", "title": title, "season": season, "missing": missing, "mode": mode, "page": page - 1})
        nav_btns.append(types.InlineKeyboardButton(text="⬅️ 上一页", callback_data=prev_k))
    if page < total_pages - 1:
        next_k = register_cb_payload("ign", {"act": "menu", "title": title, "season": season, "missing": missing, "mode": mode, "page": page + 1})
        nav_btns.append(types.InlineKeyboardButton(text="下一页 ➡️", callback_data=next_k))
    if nav_btns:
        builder.row(*nav_btns)

    # Global options
    all_k = register_cb_payload("ign", {"act": "all", "title": title, "season": season, "mode": mode})
    builder.row(types.InlineKeyboardButton(text="🛑 关闭全季缺集提醒 (整季不提醒)", callback_data=all_k))
    builder.row(types.InlineKeyboardButton(text="🔙 返回缺集雷达", callback_data=f"menu:radar:{mode}"))
    try:
        await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except Exception as exc:
        logger.exception("Telegram menu edit failed: %s", exc)
        await call.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("menu:ignored_list"))
async def cb_ignored_list(call: types.CallbackQuery, answer_callback: bool = True):
    if answer_callback:
        await call.answer()
    parts = call.data.split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    items = await DatabaseService.list_ignored()
    if not items:
        text = (
            "📋 <b>已关闭/忽略缺集清单</b>\n\n"
            "目前暂无任何主动关闭的缺集规则，所有缺集均在正常监控中。"
        )
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="🔙 返回缺集雷达", callback_data="menu:radar:LATEST"))
        try:
            await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception as exc:
            logger.exception("Telegram menu edit failed: %s", exc)
            await call.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        return

    grouped: Dict[Tuple[str, int], List[int]] = {}
    for it in items:
        k = (it["title"], int(it["season"]))
        grouped.setdefault(k, []).append(int(it["episode"]))
    groups = sorted(grouped.items(), key=lambda pair: (pair[0][0], pair[0][1]))
    page_size = 12
    total_pages = max(1, (len(groups) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    page_groups = groups[page * page_size : (page + 1) * page_size]

    text = (
        "📋 <b>已主动关闭/忽略的缺集规则清单：</b>\n\n"
        f"📄 第 <code>{page + 1}/{total_pages}</code> 页，共 <code>{len(groups)}</code> 部\n"
    )
    builder = InlineKeyboardBuilder()
    for (t, s), eps in page_groups:
        if 0 in eps:
            ep_desc = "整季缺集全部关闭"
        else:
            sorted_eps = sorted(set(eps))
            if len(sorted_eps) <= 6:
                ep_desc = "已忽略 " + ", ".join(f"E{e:02d}" for e in sorted_eps)
            else:
                ep_desc = f"已忽略 {len(sorted_eps)} 集 (E{min(sorted_eps):02d}~E{max(sorted_eps):02d})"
        text += f" • <b>《{escape(t)}》</b> 第 {s} 季 ➔ <code>{ep_desc}</code>\n"
        k = register_cb_payload(
            "ign", {"act": "unignore_all", "title": t, "season": s, "list_page": page}
        )
        builder.button(text=f"🔄 恢复《{t[:6]}》S{s}", callback_data=k)

    builder.adjust(1)
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton(text="⬅️ 上一页", callback_data=f"menu:ignored_list:{page - 1}"))
    if page < total_pages - 1:
        nav.append(types.InlineKeyboardButton(text="下一页 ➡️", callback_data=f"menu:ignored_list:{page + 1}"))
    if nav:
        builder.row(*nav)
    clear_all_k = register_cb_payload("ign", {"act": "clear_all", "list_page": page})
    builder.row(types.InlineKeyboardButton(text="🗑️ 清空所有忽略规则 (全部恢复)", callback_data=clear_all_k))
    builder.row(types.InlineKeyboardButton(text="🔙 返回缺集雷达", callback_data="menu:radar:LATEST"))
    try:
        await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except Exception as exc:
        logger.exception("Telegram menu edit failed: %s", exc)
        await call.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


# ==================== 4. 快捷追更与模式设定 (Short Key Callback) ====================\n

@dp.callback_query(F.data.startswith("fq:"))
async def cb_follow_quick(call: types.CallbackQuery):
    await call.answer()
    key = call.data[3:]
    payload = get_cb_payload(key)
    if not payload:
        await call.message.answer("⚠️ 该操作按钮已过期，请重新打开菜单选择。", parse_mode="HTML")
        return

    title = payload["title"]
    season = int(payload.get("season") or 1)
    poster = payload.get("poster")

    await DatabaseService.add_subscription(
        title=title,
        season=season,
        poster_url=poster,
        user_id=call.from_user.id,
        source="sztv",
        follow_mode="LATEST",
    )

    text = (
        f"✅ <b>成功加入追更清单！</b>\n\n"
        f"🎬 <b>剧名：</b>《{escape(title)}》第 {season} 季\n"
        f"⚙️ <b>追更模式：</b><code>⚡ 仅追最新 (现有集数续更)</code>\n"
        f"💡 小助手已将该剧加入雷达监控，一旦检测到新集数播出，将自动从锁定影视库打捞并转存入库！\n\n"
        f"<i>注：您可以在「📺 我的追更清单」中随时切换为【🔄 全量补齐】模式。</i>"
    )
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="📺 查看追更清单", callback_data="menu:my_follow"),
        types.InlineKeyboardButton(text="🔙 继续浏览日历", callback_data="calendar_menu"),
    )
    await call.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


# ==================== 5. 快捷打捞与同链接增量转存 (Short Key Callback) ====================\n

@dp.callback_query(F.data.startswith("sq:"))
async def cb_scout_quick(call: types.CallbackQuery, bot: Bot):
    await call.answer("🔍 正在启动锁定影视库定向检索...", show_alert=False)
    key = call.data[3:]
    payload = get_cb_payload(key)
    if not payload:
        await call.message.answer("⚠️ 该操作按钮已过期，请重新点击刷新。", parse_mode="HTML")
        return

    title = payload["title"]
    season = int(payload.get("season") or 1)
    target_eps = payload.get("target_eps") or []

    if not target_eps:
        library_entries = await LibraryService.get_transferred_library()
        match = LibraryService.match_show_in_library(library_entries, title, season)
        if match:
            collected = match.get("episodes") or []
            max_coll = max(collected) if collected else 0
            target_eps = [max_coll + 1] if max_coll > 0 else [1]
        else:
            target_eps = [1]

    sub = {
        "title": title,
        "season": season,
        "id": None,
        "target_eps": target_eps,
        "follow_mode": payload.get("mode") or "LATEST",
    }
    res = await ScoutService.scout_missing_episodes_for_sub(sub, bot=bot)
    st = res.get("status")

    if st == "found":
        cand = res.get("best_candidate") or {}
        pushed_eps = res.get("pushed_episodes") or []
        ep_s = ", ".join(f"E{e:02d}" for e in pushed_eps) if pushed_eps else "缺集"
        safe_provider = escape(str(cand.get("provider") or "未知"))
        safe_url = escape(str(cand.get("url") or ""))
        await call.message.answer(
            f"📥 <b>已提交转存任务</b>\n\n"
            f"🎬 <b>剧名：</b>《{escape(title)}》第 {season} 季\n"
            f"📌 <b>目标集数：</b>{ep_s}\n"
            f"💾 <b>网盘类型：</b><code>{safe_provider}</code>\n"
            f"🔗 <b>链接：</b><code>{safe_url}</code>\n"
            f"💡 任务已提交给转存服务处理；转存成功由频道广播推送，若转存失败统一由转存机器人推送告警。",
            parse_mode="HTML"
        )
    elif st == "already_ingested":
        cand = res.get("best_candidate") or {}
        safe_url = escape(str(cand.get("url") or ""))
        await call.message.answer(
            f"🛡️ <b>防重复拦截：该资源此前已收录入库</b>\n\n"
            f"🎬 <b>剧名：</b>《{escape(title)}》第 {season} 季\n"
            f"🔗 <b>链接：</b><code>{safe_url}</code>\n"
            f"💡 该分享此前已成功转存至网盘对应分类目录，系统已自动拦截，避免重复创建任务！",
            parse_mode="HTML"
        )
    elif st == "ignored":
        await call.message.answer(
            f"🚫 <b>该剧缺集已主动关闭</b>\n\n"
            f"🎬 <b>剧名：</b>《{escape(title)}》第 {season} 季\n"
            f"💡 该剧的缺集提醒已被您主动关闭或忽略，系统已跳过打捞。如需恢复请在缺集雷达中点击【恢复监控】。",
            parse_mode="HTML"
        )
    elif st == "push_failed":
        err_msg = res.get("error") or res.get("message") or "推送转存服务异常"
        cb_retry = register_cb_payload("sq", payload)
        builder = InlineKeyboardBuilder()
        builder.row(
            types.InlineKeyboardButton(text="🔄 重新打捞", callback_data=cb_retry),
            types.InlineKeyboardButton(text="🔙 返回主菜单", callback_data="menu:overview"),
        )
        await call.message.answer(
            f"⚠️ <b>打捞推送失败</b>\n\n"
            f"🎬 <b>剧名：</b>《{escape(title)}》第 {season} 季\n"
            f"❌ <b>失败原因：</b><code>{escape(err_msg)}</code>\n\n"
            f"💡 追新服务推送转存接口暂未成功，后台将自动重试打捞，您也可以稍后点击重新打捞。",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    else:
        await call.message.answer(
            f"📡 <b>暂未发现新分享链接</b>\n\n"
            f"在锁定影视频道/群聊历史中暂未检索到《{escape(title)}》第 {season} 季对应缺集的新有效分享。\n"
            f"💡 <b>持续监控中：</b> 后台追更雷达已将该剧锁定，一旦监听群/频道出现新资源，将秒级自动打捞转存！",
            parse_mode="HTML"
        )


# ==================== 5.1 失败告警交互按钮回调 (Retry / Cancel / Rescout) ====================

@dp.callback_query(F.data.startswith("tx_fail_retry:"))
async def cb_tx_fail_retry(call: types.CallbackQuery, bot: Bot):
    user_id = call.from_user.id if call.from_user else 0
    if ADMIN_TG_ID and user_id != ADMIN_TG_ID:
        await call.answer("⛔ 只有管理员才能操作重试", show_alert=True)
        return
    try:
        job_id = int(str(call.data).split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("无效的任务编号", show_alert=True)
        return
    await call.answer("🔄 正在请求重新排队执行转存...", show_alert=False)
    from live_status_monitor import retry_transfer_job
    res = await retry_transfer_job(job_id)
    if res.get("success"):
        builder = InlineKeyboardBuilder()
        builder.row(
            types.InlineKeyboardButton(text="🛑 取消该任务", callback_data=f"tx_fail_cancel:{job_id}"),
            types.InlineKeyboardButton(text="🔙 返回主菜单", callback_data="menu:overview"),
        )
        old_text = getattr(call.message, "html_text", None) or (call.message.text or "")
        new_text = old_text + f"\n\n<b>[状态更新]</b> 🔄 <b>转存任务 #{job_id} 已成功重新排队！</b>\nWorker 正在重试转存与入库。"
        try:
            await call.message.edit_text(new_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception:
            await call.message.answer(f"🔄 <b>转存任务 #{job_id} 已重新排队！</b>\nWorker 将自动重试转存。", reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        err = res.get("error") or "未知错误"
        await call.message.answer(f"⚠️ <b>重试排队失败：</b>{escape(err)}", parse_mode="HTML")


@dp.callback_query(F.data.startswith("tx_fail_cancel:"))
async def cb_tx_fail_cancel(call: types.CallbackQuery):
    user_id = call.from_user.id if call.from_user else 0
    if ADMIN_TG_ID and user_id != ADMIN_TG_ID:
        await call.answer("⛔ 只有管理员才能操作取消", show_alert=True)
        return
    try:
        job_id = int(str(call.data).split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("无效的任务编号", show_alert=True)
        return
    await call.answer("🛑 正在取消该转存任务...", show_alert=False)
    from live_status_monitor import cancel_transfer_job
    res = await cancel_transfer_job(job_id, reason="管理员在追新机器人中手动取消")
    if res.get("success"):
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="🔙 返回主菜单", callback_data="menu:overview"))
        old_text = getattr(call.message, "html_text", None) or (call.message.text or "")
        new_text = old_text + f"\n\n<b>[状态更新]</b> 🛑 <b>转存任务 #{job_id} 已被您手动取消。</b>\n系统已停止重试该资源。"
        try:
            await call.message.edit_text(new_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception:
            await call.message.answer(f"🛑 <b>转存任务 #{job_id} 已手动取消。</b>", reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        err = res.get("error") or "未知错误"
        await call.message.answer(f"⚠️ <b>取消失败：</b>{escape(err)}", parse_mode="HTML")


@dp.callback_query(F.data.startswith("tx_fail_rescout:"))
async def cb_tx_fail_rescout(call: types.CallbackQuery, bot: Bot):
    user_id = call.from_user.id if call.from_user else 0
    if ADMIN_TG_ID and user_id != ADMIN_TG_ID:
        await call.answer("⛔ 只有管理员才能操作打捞", show_alert=True)
        return
    try:
        job_id = int(str(call.data).split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("无效的任务编号", show_alert=True)
        return
    await call.answer("🔍 正在重新检索群聊/频道其他可用资源...", show_alert=False)
    from live_status_monitor import get_job_info
    info = await get_job_info(job_id)
    if not info or not info.get("title"):
        await call.message.answer("⚠️ 找不到该任务对应的剧名信息，无法重新打捞。", parse_mode="HTML")
        return
    title = info["title"]
    season = info.get("season") or 1
    sub = {
        "title": title,
        "season": season,
        "id": None,
        "target_eps": [],
        "follow_mode": "LATEST",
    }
    res = await ScoutService.scout_missing_episodes_for_sub(sub, bot=bot)
    st = res.get("status")
    if st == "found":
        cand = res.get("best_candidate") or {}
        pushed_eps = res.get("pushed_episodes") or []
        ep_s = ", ".join(f"E{e:02d}" for e in pushed_eps) if pushed_eps else "缺集"
        await call.message.answer(
            f"🎉 <b>重新打捞成功并已提交转存！</b>\n\n"
            f"🎬 《{escape(title)}》第 {season} 季\n"
            f"📌 目标集数：{ep_s}\n"
            f"💾 网盘：<code>{escape(cand.get('provider') or '未知')}</code>\n"
            f"🔗 新链接：<code>{escape(cand.get('url') or '')}</code>",
            parse_mode="HTML"
        )
    elif st == "already_ingested":
        await call.message.answer(f"🛡️ 该剧打捞到的新链接已在转存队列或已入库，无需重复转存。", parse_mode="HTML")
    else:
        msg = res.get("message") or "暂未发现其他可用分享"
        await call.message.answer(f"📡 <b>重新打捞结果：</b>\n{escape(msg)}", parse_mode="HTML")


@dp.callback_query(F.data.startswith("tx_fail_ignore:"))
async def cb_tx_fail_ignore(call: types.CallbackQuery):
    user_id = call.from_user.id if call.from_user else 0
    if ADMIN_TG_ID and user_id != ADMIN_TG_ID:
        await call.answer("⛔ 只有管理员才能操作", show_alert=True)
        return
    try:
        job_id = int(str(call.data).split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("无效的任务编号", show_alert=True)
        return
    from live_status_monitor import get_job_info
    info = await get_job_info(job_id)
    if not info or not info.get("title"):
        await call.answer("⚠️ 找不到该任务的剧集信息", show_alert=True)
        return
    title = info["title"]
    season = info.get("season") or 1
    await DatabaseService.ignore_missing(title, season, 0)
    await call.answer(f"已忽略《{title}》全剧更新提醒与自动打捞", show_alert=True)
    try:
        old_text = getattr(call.message, "html_text", None) or (call.message.text or "")
        new_text = old_text + f"\n\n<b>[状态更新]</b> 🚫 <b>已主动忽略《{escape(title)}》第 {season} 季</b>，后台不再对其提醒和打捞。"
        await call.message.edit_text(new_text, parse_mode="HTML")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("tx_sp_retry:"))
async def cb_tx_sp_retry(call: types.CallbackQuery, bot: Bot):
    user_id = call.from_user.id if call.from_user else 0
    if ADMIN_TG_ID and user_id != ADMIN_TG_ID:
        await call.answer("⛔ 只有管理员才能操作", show_alert=True)
        return
    try:
        push_id = int(str(call.data).split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("无效的推送编号", show_alert=True)
        return
    await call.answer("🔄 正在重新尝试推送转存接口...", show_alert=False)
    try:
        from live_status_monitor import retry_failed_scout_push
        res = await retry_failed_scout_push(push_id, bot=bot)
        if res.get("success"):
            builder = InlineKeyboardBuilder()
            builder.row(types.InlineKeyboardButton(text="🔙 返回主菜单", callback_data="menu:overview"))
            old_text = getattr(call.message, "html_text", None) or (call.message.text or "")
            new_text = old_text + f"\n\n<b>[状态更新]</b> ✅ <b>推送重试成功！已成功送入转存任务队列。</b>"
            try:
                await call.message.edit_text(new_text, reply_markup=builder.as_markup(), parse_mode="HTML")
            except Exception:
                await call.message.answer("✅ <b>推送重试成功！已成功送入转存任务队列。</b>", parse_mode="HTML")
        else:
            err = res.get("error") or "重试失败"
            await call.message.answer(f"⚠️ <b>重试推送失败：</b>{escape(err)}", parse_mode="HTML")
    except Exception as e:
        logger.exception("cb_tx_sp_retry crashed for push #%d", push_id)
        try:
            await call.message.answer(f"⚠️ <b>重试按钮异常：</b>{escape(str(e))}", parse_mode="HTML")
        except Exception:
            pass


@dp.callback_query(F.data.startswith("tx_sp_cancel:"))
async def cb_tx_sp_cancel(call: types.CallbackQuery):
    user_id = call.from_user.id if call.from_user else 0
    if ADMIN_TG_ID and user_id != ADMIN_TG_ID:
        await call.answer("⛔ 只有管理员才能操作", show_alert=True)
        return
    try:
        push_id = int(str(call.data).split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("无效的推送编号", show_alert=True)
        return
    await call.answer("🛑 正在取消该推送记录...", show_alert=False)
    try:
        from live_status_monitor import cancel_failed_scout_push
        res = await cancel_failed_scout_push(push_id)
        if res.get("success"):
            old_text = getattr(call.message, "html_text", None) or (call.message.text or "")
            new_text = old_text + f"\n\n<b>[状态更新]</b> 🛑 <b>已取消该推送任务。</b>"
            try:
                await call.message.edit_text(new_text, parse_mode="HTML")
            except Exception:
                await call.message.answer("🛑 <b>已取消该推送任务。</b>", parse_mode="HTML")
        else:
            await call.message.answer(f"⚠️ 取消失败：{escape(res.get('error') or '未知错误')}", parse_mode="HTML")
    except Exception as e:
        logger.exception("cb_tx_sp_cancel crashed for push #%d", push_id)
        try:
            await call.message.answer(f"⚠️ <b>取消按钮异常：</b>{escape(str(e))}", parse_mode="HTML")
        except Exception:
            pass


@dp.callback_query(F.data.startswith("tx_sp_rescout:"))
async def cb_tx_sp_rescout(call: types.CallbackQuery, bot: Bot):
    user_id = call.from_user.id if call.from_user else 0
    if ADMIN_TG_ID and user_id != ADMIN_TG_ID:
        await call.answer("⛔ 只有管理员才能操作", show_alert=True)
        return
    try:
        push_id = int(str(call.data).split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("无效的推送编号", show_alert=True)
        return
    await call.answer("🔍 正在重新检索新资源...", show_alert=False)
    from database import DatabaseService
    row = await DatabaseService.get_failed_scout_push(push_id)
    if not row:
        await call.message.answer("⚠️ 找不到该推送记录", parse_mode="HTML")
        return
    title = row["title"]
    season = row["season"]
    sub = {
        "title": title,
        "season": season,
        "id": None,
        "target_eps": row.get("episodes") or [],
        "follow_mode": "LATEST",
    }
    res = await ScoutService.scout_missing_episodes_for_sub(sub, bot=bot)
    st = res.get("status")
    if st == "found":
        cand = res.get("best_candidate") or {}
        await call.message.answer(
            f"🎉 <b>重新打捞成功并已提交转存！</b>\n\n"
            f"🎬 《{escape(title)}》第 {season} 季\n"
            f"💾 网盘：<code>{escape(cand.get('provider') or '未知')}</code>\n"
            f"🔗 链接：<code>{escape(cand.get('url') or '')}</code>",
            parse_mode="HTML"
        )
    else:
        msg = res.get("message") or "未发现其他可用分享"
        await call.message.answer(f"📡 <b>打捞结果：</b>{escape(msg)}", parse_mode="HTML")


@dp.callback_query(F.data.startswith("tx_sp_ignore:"))
async def cb_tx_sp_ignore(call: types.CallbackQuery):
    user_id = call.from_user.id if call.from_user else 0
    if ADMIN_TG_ID and user_id != ADMIN_TG_ID:
        await call.answer("⛔ 只有管理员才能操作", show_alert=True)
        return
    try:
        push_id = int(str(call.data).split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("无效的推送编号", show_alert=True)
        return
    from database import DatabaseService
    row = await DatabaseService.get_failed_scout_push(push_id)
    if not row:
        await call.answer("⚠️ 找不到该推送记录", show_alert=True)
        return
    title = row["title"]
    season = row["season"]
    await DatabaseService.ignore_missing(title, season, 0)
    await call.answer(f"已忽略《{title}》全剧更新提醒与自动打捞", show_alert=True)
    try:
        old_text = getattr(call.message, "html_text", None) or (call.message.text or "")
        new_text = old_text + f"\n\n<b>[状态更新]</b> 🚫 <b>已主动忽略《{escape(title)}》第 {season} 季</b>，后台不再对其提醒和打捞。"
        await call.message.edit_text(new_text, parse_mode="HTML")
    except Exception:
        pass



# ==================== 6. 我的追更清单与管理 ====================

@dp.message(Command("follow"))
@dp.callback_query(F.data.startswith("menu:my_follow"))
async def show_my_follow(
    event: types.Message | types.CallbackQuery,
    answer_callback: bool = True,
    target_page: Optional[int] = None,
):
    user_id = event.from_user.id
    page = target_page or 1
    if isinstance(event, types.CallbackQuery):
        if answer_callback:
            await event.answer()
        parts = event.data.split(":")
        if len(parts) >= 3 and parts[2].isdigit():
            page = int(parts[2])

    subs = await DatabaseService.list_subscriptions(user_id)
    total_items = len(subs)
    PAGE_SIZE = 6
    total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))

    header_line = f"您当前共关注了 <b>{total_items}</b> 部在更/缺集剧集 (第 {page}/{total_pages} 页)："
    text = (
        "📺 <b>我的追剧清单</b>\n\n"
        + f"{header_line}\n"
        + "──────────────────────\n"
    )
    builder = InlineKeyboardBuilder()

    if not subs:
        text += (
            "<i>您目前还没有添加任何追更剧集哦！</i>\n\n"
            "您可以前往「📅 追剧日历」或使用「🔍 搜索剧名加追」添加想看的新剧~"
        )
    else:
        start_idx = (page - 1) * PAGE_SIZE
        page_subs = subs[start_idx : start_idx + PAGE_SIZE]

        for idx, sub in enumerate(page_subs, start=start_idx + 1):
            t = sub["title"]
            sea = sub["season"]
            sid = sub["id"]
            tot = sub.get("total_episodes")
            tot_str = f"共 {tot} 集" if tot else "连载中"
            mode = sub.get("follow_mode") or "LATEST"
            mode_badge = "⚡ 仅追最新" if mode == "LATEST" else "🔄 全量补齐"
            coll = sub.get("collected_episodes") or []
            coll_str = f"已转存 {len(coll)} 集" if coll else "暂未入库"

            text += (
                f"{idx}. <b>《{escape(t)}》</b> 第 {sea} 季 ({tot_str})\n"
                f"   └ 模式：<code>{mode_badge}</code> | 状态：{coll_str}\n"
            )
            k_toggle = f"sub_toggle:{sid}:{page}"
            k_del = f"sub_del:{sid}:{page}"
            builder.row(
                types.InlineKeyboardButton(text=f"🔄 切换《{t[:6]}》", callback_data=k_toggle),
                types.InlineKeyboardButton(text="🗑️ 取消追更", callback_data=k_del),
            )

        if total_pages > 1:
            nav_row = []
            if page > 1:
                nav_row.append(types.InlineKeyboardButton(text="◀️ 上一页", callback_data=f"menu:my_follow:{page-1}"))
            nav_row.append(types.InlineKeyboardButton(text=f"📄 {page}/{total_pages}", callback_data="noop"))
            if page < total_pages:
                nav_row.append(types.InlineKeyboardButton(text="下一页 ▶️", callback_data=f"menu:my_follow:{page+1}"))
            builder.row(*nav_row)

    builder.row(
        types.InlineKeyboardButton(text="📅 追剧日历", callback_data="calendar_menu"),
        types.InlineKeyboardButton(text="📡 缺集雷达", callback_data="menu:radar:LATEST"),
    )
    builder.row(
        types.InlineKeyboardButton(text="🔍 搜索剧名", callback_data="menu:search_prompt"),
        types.InlineKeyboardButton(text="🔙 返回主菜单", callback_data="menu:overview"),
    )

    if isinstance(event, types.CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except TelegramBadRequest as exc:
            logger.warning("Telegram message edit failed: %s", exc)
    else:
        await event.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("sub_toggle:"))
async def cb_sub_toggle(call: types.CallbackQuery):
    parts = call.data.split(":")
    sub_id = int(parts[1])
    page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1
    new_mode = await DatabaseService.toggle_follow_mode(sub_id)
    mode_str = "⚡ 仅追最新" if new_mode == "LATEST" else "🔄 全量补齐"
    await call.answer(f"✅ 已切换追更模式为：{mode_str}", show_alert=True)
    await show_my_follow(call, answer_callback=False, target_page=page)


@dp.callback_query(F.data.startswith("sub_del:"))
async def cb_sub_delete(call: types.CallbackQuery):
    parts = call.data.split(":")
    sub_id = int(parts[1])
    page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1
    await DatabaseService.delete_subscription(sub_id)
    await call.answer("🗑️ 已取消对该剧的追更关注。", show_alert=True)
    await show_my_follow(call, answer_callback=False, target_page=page)


@dp.callback_query(F.data == "noop")
async def cb_noop(call: types.CallbackQuery):
    await call.answer()


@dp.callback_query(F.data == "menu:manage")
async def cb_manage_watchlist(call: types.CallbackQuery):
    await call.answer()
    await show_my_follow(call, answer_callback=False)

# ==================== 6.5 热播剧集全自动追新入库管理 ====================

ALL_AUTO_INGEST_CATS = {
    "domestic": "🇨🇳 国产剧集",
    "anime": "🌸 动画新番",
    "western": "🇺🇸 欧美剧集",
    "jp-kr": "🇯🇵 日韩剧集",
    "movie": "🎬 电影上映",
}

@dp.callback_query(F.data == "menu:auto_ingest")
async def show_auto_ingest_menu(
    event: types.Message | types.CallbackQuery,
    answer_callback: bool = True,
):
    if isinstance(event, types.CallbackQuery) and answer_callback:
        await event.answer()
    enabled = await DatabaseService.is_auto_ingest_enabled()
    cats = await DatabaseService.get_auto_ingest_categories()

    st_badge = "🟢 已开启全自动入库" if enabled else "🔴 已暂停自动入库"
    st_desc = "每小时自动对齐数字影视当日排期，命中 4 大锁定频道的有效网盘后秒级推入转存！" if enabled else "自动入库已关闭，仅在您手动点击打捞或加入追更时处理。"

    cat_status_lines = []
    for k, v in ALL_AUTO_INGEST_CATS.items():
        check = "✅ 已启用" if k in cats else "❌ 已停用"
        cat_status_lines.append(f"  • {v}：{check}")

    text = (
        "🔥 <b>热播剧集全自动追新入库管理</b>\n\n"
        f"⚙️ <b>当前状态：</b><b>{st_badge}</b>\n"
        f"💡 <i>{st_desc}</i>\n\n"
        "📂 <b>监控分类设置：</b>\n"
        + "\n".join(cat_status_lines)
        + "\n\n"
        "🎯 <b>资源渠道：</b>TG 影视监控群/频道 + 帧影(FrameHdr)精选站\n"
        "💾 <b>网盘策略：</b>光鸭云盘绝对第一优先\n"
        "🚀 <b>增量自适应：</b>遇新出集数自动扩容放行，无需人工干预！"
    )

    builder = InlineKeyboardBuilder()
    toggle_text = "🛑 暂停自动入库" if enabled else "▶️ 开启自动入库"
    builder.row(
        types.InlineKeyboardButton(text=toggle_text, callback_data="auto_ingest:toggle"),
        types.InlineKeyboardButton(text="⚡ 立即执行一轮热播入库", callback_data="auto_ingest:run_now"),
    )
    builder.row(
        types.InlineKeyboardButton(text="📋 今日热播排期清单", callback_data="auto_ingest:today_shows"),
        types.InlineKeyboardButton(text="🎯 查看已命中入库记录", callback_data="auto_ingest:history"),
    )

    cat_btns = []
    for k, v in ALL_AUTO_INGEST_CATS.items():
        icon = "✅" if k in cats else "⬜"
        cat_btns.append(types.InlineKeyboardButton(text=f"{icon} {v[:6]}", callback_data=f"auto_ingest:cat:{k}"))
    builder.row(cat_btns[0], cat_btns[1])
    builder.row(cat_btns[2], cat_btns[3])
    builder.row(cat_btns[4])

    builder.row(
        types.InlineKeyboardButton(text="🔙 返回主菜单", callback_data="menu:overview")
    )

    if isinstance(event, types.CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception as exc:
            logger.exception("Telegram menu edit failed: %s", exc)
            await event.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data == "auto_ingest:toggle")
async def cb_toggle_auto_ingest(call: types.CallbackQuery):
    new_st = await DatabaseService.toggle_auto_ingest_enabled()
    msg = "✅ 已开启热播剧集全自动入库！" if new_st else "🛑 已暂停热播剧集自动入库。"
    await call.answer(msg, show_alert=True)
    await show_auto_ingest_menu(call, answer_callback=False)


@dp.callback_query(F.data.startswith("auto_ingest:cat:"))
async def cb_toggle_auto_ingest_cat(call: types.CallbackQuery):
    cat_key = call.data.split(":")[2]
    await DatabaseService.toggle_auto_ingest_category(cat_key)
    c_name = ALL_AUTO_INGEST_CATS.get(cat_key, cat_key)
    await call.answer(f"已更新分类：{c_name}", show_alert=False)
    await show_auto_ingest_menu(call, answer_callback=False)


@dp.callback_query(F.data == "auto_ingest:run_now")
async def cb_run_auto_ingest_now(call: types.CallbackQuery, bot: Bot):
    await call.answer("⚡ 正在扫描今日热播并执行自动入库...", show_alert=False)
    loading_msg = await call.message.answer("⚡ <b>正在从数字影视日历提取今日热播，并比对 5 大目标频道网盘资源...</b>", parse_mode="HTML")

    res = await ScoutService.auto_ingest_hot_calendar_shows(bot=bot)
    await loading_msg.delete()

    pushed_cnt = res.get("pushed_count", 0)
    scanned_cnt = res.get("total_scanned", 0)
    pushed_shows = res.get("pushed_shows", [])

    if pushed_cnt > 0:
        lines = []
        for item in pushed_shows[:8]:
            ep_s = ",".join(f"E{e:02d}" for e in item.get("episodes", []))
            lines.append(f" • <b>《{escape(item['title'])}》</b> S{item['season']} <code>{ep_s}</code> ({item['provider']})")
        shows_text = "\n".join(lines)
        text = (
            f"🎉 <b>热播自动入库执行完毕！</b>\n\n"
            f"📊 巡检排期：<code>{scanned_cnt}</code> 部今日热播剧集\n"
            f"🎯 <b>已提交转存任务：</b><code>{pushed_cnt}</code> 部\n\n"
            f"{shows_text}\n\n"
            "✨ 任务已交给转存频道处理；成功结果由转存频道推送，失败由追新机器人告警。"
        )
    else:
        text = (
            f"✅ <b>热播巡检完毕：当前无新增待收剧集</b>\n\n"
            f"共核验 <code>{scanned_cnt}</code> 部排期影视，库中均已拥有或暂无新增分享。\n"
            "后台定时雷达将持续盯盘，一旦频道出现新分享将秒级自动转存！"
        )

    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="📡 查看缺集雷达", callback_data="menu:radar:LATEST"),
        types.InlineKeyboardButton(text="🔙 返回管理面板", callback_data="menu:auto_ingest"),
    )
    await call.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data == "auto_ingest:today_shows")
async def cb_view_today_hot_shows(call: types.CallbackQuery):
    await call.answer("🔍 正在获取今日全网热播与排期清单...", show_alert=False)
    active_cats = await DatabaseService.get_auto_ingest_categories()
    library = await LibraryService.get_transferred_library()
    
    sections = []
    total_shows = 0
    for cat_key in active_cats:
        c_name = ALL_AUTO_INGEST_CATS.get(cat_key, cat_key)
        cat_data = await CalendarService.get_shows_for_category_and_date(cat_key, date_offset=0)
        shows = cat_data.get("shows", [])
        if not shows:
            continue
        valid_shows = [s for s in shows if (s.get("episodes") or [])]
        if not valid_shows:
            continue
        total_shows += len(valid_shows)
        lines = [f"<b>【{c_name}】</b> (共 {len(valid_shows)} 部)"]
        for s in valid_shows[:10]:
            t = s["title"]
            sea = s.get("season", 1)
            eps = s.get("episodes", [])
            ep_str = ",".join(f"E{e:02d}" for e in eps) if eps else s.get("ep_display", "全集")
            
            # Match status in library
            match = LibraryService.match_show_in_library(library, t, sea)
            if match:
                coll = match.get("episodes", [])
                if set(eps).issubset(set(coll)):
                    status_badge = "✅ 库中已集齐"
                else:
                    status_badge = "🚨 库中缺当期集"
            else:
                status_badge = "🆕 媒体库未收录"
            lines.append(f"  • <b>《{escape(t)}》</b> S{sea} <code>{ep_str}</code> · {status_badge}")
        if len(shows) > 10:
            lines.append(f"  <i>...等其余 {len(shows) - 10} 部</i>")
        sections.append("\n".join(lines))

    header = (
        f"📋 <b>今日全网监控热播清单</b> (共覆盖 <code>{total_shows}</code> 部)\\n\\n"
        "💡 <i>系统每小时自动扫描上述剧集，一旦 4 大频道有新分享且命中待补集数，将秒级自动转存入库！</i>\\n\\n"
    )
    content = header + "\n\n".join(sections) if sections else header + "暂无开启分类的今日排期数据。"

    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="🎯 查看已自动命中入库历史", callback_data="auto_ingest:history"),
        types.InlineKeyboardButton(text="⚡ 立即执行一轮转存", callback_data="auto_ingest:run_now"),
    )
    builder.row(types.InlineKeyboardButton(text="🔙 返回热播面板", callback_data="menu:auto_ingest"))
    
    try:
        await call.message.edit_text(content, reply_markup=builder.as_markup(), parse_mode="HTML")
    except Exception as exc:
        logger.exception("Telegram menu edit failed: %s", exc)
        await call.message.answer(content, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data == "auto_ingest:history")
async def cb_view_auto_ingest_history(call: types.CallbackQuery):
    await call.answer("🔍 正在拉取真实转存入库审计记录...", show_alert=False)
    from live_status_monitor import get_live_ingest_history
    history = await get_live_ingest_history(limit=15)
    if not history:
        text = (
            "🎯 <b>热播自动命中与转存入库历史</b>\n\n"
            "暂无自动命中记录。当后台自动巡检命中频道分享并推入转存后，会在此实时列出明细！"
        )
    else:
        lines = []
        for h in history:
            t = escape(h["title"])
            s = h["season"]
            prov = h.get("provider", "guangya")
            st = h.get("status")
            t_str = h.get("created_at_cst")
            if st in ("SUCCESS", "TRANSFERRED"):
                st_badge = "✅ 已成功入库"
            elif st in ("RUNNING", "QUEUED", "READY"):
                st_badge = "🔄 云端转存中..."
            elif st == "FAILED":
                err = h.get("error_message") or "云端转存超时"
                if "180 秒内仅确认 0" in err:
                    err = "网盘确认超时 (文件过多或服务端排队)"
                elif "由另一个进程处理" in err:
                    err = "同批任务并发冲突保护"
                elif len(err) > 32:
                    err = err[:30] + "..."
                st_badge = f"❌ 转存失败 ({escape(err)})"
            elif st == "DUPLICATE":
                st_badge = "🛡️ 防重跳过 (此前已转存)"
            else:
                st_badge = f"⏳ 状态: {st}"

            lines.append(
                f"• <b>《{t}》</b> 第 {s} 季\n"
                f"  └ 运行态：<b>{st_badge}</b>\n"
                f"  └ 🕒 <code>{t_str}</code> | 🏷️ <code>{prov}</code> | 🔗 <a href=\"{h['share_url']}\">分享源</a>"
            )
        text = (
            f"🎯 <b>热播自动命中与转存真实战报</b> (最近 {len(history)} 条)\n\n"
            + "\n\n".join(lines) + "\n\n"
            "💡 <i>数据直连转存引擎审计底账，真实呈现入库成功、进行中与失败原因！</i>"
        )

    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="📋 查看今日热播清单", callback_data="auto_ingest:today_shows"),
        types.InlineKeyboardButton(text="⚡ 立即执行一轮转存", callback_data="auto_ingest:run_now"),
    )
    builder.row(types.InlineKeyboardButton(text="🔙 返回热播面板", callback_data="menu:auto_ingest"))

    try:
        await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML", disable_web_page_preview=True)
    except Exception as exc:
        logger.exception("Telegram menu edit failed: %s", exc)
        await call.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML", disable_web_page_preview=True)


# ==================== 7. 搜索剧名加追 ====================

@dp.callback_query(F.data == "menu:search_prompt")
async def cb_search_prompt(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(SearchFSM.waiting_for_query)
    text = (
        "🔍 <b>搜索剧集并添加追更</b>\n\n"
        "请在下方直接发送您想要关注的<b>剧集名称</b>（例如：<code>仙逆</code>、<code>黑袍纠察队</code>、<code>白夜追凶</code>）："
    )
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🔙 取消返回", callback_data="menu:overview"))
    try:
        await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except Exception as exc:
        logger.exception("Telegram menu edit failed: %s", exc)
        await call.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.message(SearchFSM.waiting_for_query)
async def process_search_query(message: types.Message, state: FSMContext):
    await state.clear()
    query = (message.text or "").strip()
    if not query:
        await message.answer("⚠️ 搜索内容不能为空，请重新发送。")
        return

    loading = await message.answer(f"🔍 正在检索《{escape(query)}》...")
    candidates = await CalendarService.search_tmdb(query)
    await loading.delete()

    if not candidates:
        builder = InlineKeyboardBuilder()
        builder.row(
            types.InlineKeyboardButton(text="🔍 重新搜索", callback_data="menu:search_prompt"),
            types.InlineKeyboardButton(text="🔙 返回主菜单", callback_data="menu:overview"),
        )
        await message.answer(f"未找到与 <b>{escape(query)}</b> 相关的影视条目，请换个关键词试试~", reply_markup=builder.as_markup(), parse_mode="HTML")
        return

    text = f"🔍 为您找到与 <b>{escape(query)}</b> 相关的剧集条目：\n\n"
    builder = InlineKeyboardBuilder()

    for idx, cand in enumerate(candidates[:5], start=1):
        t = cand["title"]
        y = cand.get("year") or "未知年份"
        ov = cand.get("overview") or "暂无简介"
        text += f"{idx}. <b>《{escape(t)}》</b> ({y})\n   📝 {escape(ov[:80])}...\n\n"
        cb_key = register_cb_payload("fq", {"title": t, "season": 1, "poster": cand.get("poster_path"), "cat": "domestic", "offset": 0, "page": 1})
        builder.button(text=f"➕ 追更《{t[:8]}》第 1 季", callback_data=cb_key)

    builder.adjust(1)
    builder.row(
        types.InlineKeyboardButton(text="🔍 换个关键词", callback_data="menu:search_prompt"),
        types.InlineKeyboardButton(text="🔙 返回主菜单", callback_data="menu:overview"),
    )
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


# ==================== 8. 全库一键打捞与后台定时雷达 ====================

@dp.message(Command("scan"))
@dp.callback_query(F.data == "menu:scan_cloud")
async def cb_scan_cloud(event: types.Message | types.CallbackQuery, bot: Bot):
    if isinstance(event, types.CallbackQuery):
        await event.answer("🔍 正在深入扫描光鸭网盘【影视转存总目录】物理文件...", show_alert=False)
        msg = event.message
    else:
        msg = await event.answer("🔍 正在深入扫描光鸭网盘【影视转存总目录】物理文件...")

    from services.cloud_inventory_service import CloudInventoryService
    res = await CloudInventoryService.scan_guangya_master()
    LibraryService.invalidate_radar_cache()

    if res.get("success"):
        purged = res.get("purged_count", 0)
        purge_note = f"\n🗑️ <b>实盘自动核销：</b>检测到手动删除了 <code>{purged}</code> 个集数，已自动释放数据库占用，雷达将自动重新打捞高质新源！\n" if purged > 0 else ""
        text = (
            "☁️ <b>网盘物理总目录扫描完成！</b>\n\n"
            f"📁 扫描范围：光鸭云盘 <code>影视转存总目录</code>\n"
            f"⏱️ 扫描耗时：<code>{res.get('scan_duration')}</code> 秒\n"
            f"🎬 识别影视：<code>{res.get('total_titles')}</code> 部（含国产剧/欧美剧/动漫/电影）\n"
            f"📼 入库文件：<code>{res.get('total_files')}</code> 个物理视频文件\n"
            f"{purge_note}\n"
            "💡 <b>双轨合一核验完成：</b>\n"
            "无论是由机器人自动转存、还是爸爸平时自己手动转存的影视剧，系统均已全部登记在册，绝不误判缺集！"
        )
    else:
        text = f"❌ 扫描网盘失败：{res.get('error')}"

    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="📡 查看最新缺集雷达", callback_data="menu:radar:LATEST"),
        types.InlineKeyboardButton(text="🏠 返回主菜单", callback_data="menu:overview")
    )
    try:
        await msg.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except Exception as exc:
        logger.exception("Telegram menu edit failed: %s", exc)
        await msg.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.message(Command("sync"))
@dp.callback_query(F.data == "menu:sync_now")
async def cb_sync_now(event: types.Message | types.CallbackQuery, bot: Bot):
    if isinstance(event, types.CallbackQuery):
        await event.answer("🔄 正在启动全库缺集打捞...", show_alert=False)
        msg = event.message
    else:
        msg = await event.answer("🔄 正在启动全库缺集打捞...")

    # 1. 立即给用户交互反馈，拒绝黑屏与假死感！
    init_text = (
        "🔄 <b>正在启动全库缺集打捞与日历对齐...</b>\n\n"
        "⏳ 正在拉取 15 大日历排期与全部在追剧集清单，请稍候..."
    )
    try:
        await msg.edit_text(init_text, parse_mode="HTML")
    except Exception:
        pass

    last_edit = [time.time()]

    async def _on_progress(done: int, total: int, title: str, pushed: int):
        now = time.time()
        if done == total or (now - last_edit[0] >= 2.5):
            last_edit[0] = now
            pct = int((done / total) * 100) if total else 100
            bar_len = 10
            filled = int((done / total) * bar_len) if total else bar_len
            bar = "█" * filled + "░" * (bar_len - filled)
            p_text = f"\n🎯 <b>已命中推送：</b><code>{pushed}</code> 部新集数" if pushed > 0 else ""
            prog_text = (
                "🔄 <b>全库追新与缺集打捞执行中...</b>\n\n"
                f"📊 <b>巡更进度：</b>[{bar}] <code>{done}/{total}</code> ({pct}%)\n"
                f"🔍 <b>当前扫描：</b>《{html.escape(title[:16])}》{p_text}\n\n"
                "⚡ 4路并发高速检索 6 大专属频道与 FrameHdr 优质源..."
            )
            try:
                await msg.edit_text(prog_text, parse_mode="HTML")
            except Exception:
                pass

    res = await ScoutService.sync_and_scout_all(bot=bot, progress_callback=_on_progress)

    pushed_list = res.get("pushed_shows") or []
    if pushed_list:
        pushed_detail = "\n🚀 <b>本次新送入转存：</b>\n"
        for ps in pushed_list[:5]:
            eps_s = ", ".join(f"E{e}" for e in ps.get("episodes", [])) if ps.get("episodes") else "新集"
            pushed_detail += f"• 《{html.escape(ps['title'])}》 S{ps.get('season', 1):02d} ({eps_s})\n"
        if len(pushed_list) > 5:
            pushed_detail += f"  <i>...及其他 {len(pushed_list)-5} 部</i>\n"
    else:
        pushed_detail = "\n✅ <b>诊断结论：</b>当前所有在追连载剧均已跟至全网最新发布进度，暂无压制组释出更新文件。\n"

    text = (
        "✅ <b>全库追新与打捞执行完成！</b>\n\n"
        f"📅 今日日历排期：对齐 <code>{res.get('calendar_shows_count')}</code> 部剧集\n"
        f"📺 正在追更剧集：全量巡更 <code>{res.get('subs_count')}</code> 部\n"
        f"🎯 已提交转存任务：<code>{res.get('pushed_count')}</code> 个新资源\n"
        f"🛡️ 历史重复链接：自动防重拦截 <code>{res.get('skipped_duplicates')}</code> 条\n"
        f"{pushed_detail}\n"
        "💡 <i>系统每小时在后台静默巡更，新集发布后将自动转存并推送至发布频道。</i>"
    )
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="📡 查看缺集雷达", callback_data="menu:radar:LATEST"),
        types.InlineKeyboardButton(text="📺 查看追更清单", callback_data="menu:my_follow"),
    )
    builder.row(
        types.InlineKeyboardButton(text="⚡ 实时转存队列", callback_data="menu:queue_status"),
        types.InlineKeyboardButton(text="🔙 返回主菜单", callback_data="menu:overview"),
    )

    try:
        await msg.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except Exception as exc:
        logger.exception("Telegram menu edit failed: %s", exc)
        await msg.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


# Worker 幂等锁：防止上一轮未完成时下一轮启动（比如手动触发+定时叠加）
# 内存字典即可，进程重启自动清零，不影响自动转存链路
_WORKER_LOCKS: Dict[str, bool] = {}

# Worker 上次运行信息（可观测性）
_WORKER_STATUS: Dict[str, Dict[str, Any]] = {}


async def periodic_radar_worker(bot: Bot):
    logger.info("ZhuiXin periodic radar worker starting...")
    _WORKER_STATUS["periodic_radar"] = {"running": False, "last_error": None, "last_run_seconds": None}
    # Pre-warm radar snapshot cache on startup so user clicks are instant (0.005s)
    try:
        data = await CalendarService.get_today_shows("domestic")
        shows = data.get("shows", [])
        await LibraryService.get_radar_summary(shows, follow_mode="LATEST", force_refresh=True)
        await LibraryService.get_radar_summary(shows, follow_mode="FULL", force_refresh=True)
        logger.info("ZhuiXinBot: Radar cache warm-up completed successfully!")
    except Exception as e:
        logger.error("Radar warm-up error on startup: %s", e, exc_info=True)

    while True:
        if _WORKER_LOCKS.get("periodic_radar"):
            logger.warning("periodic_radar already running, skipping this cycle")
            _WORKER_STATUS["periodic_radar"]["last_error"] = "skipped: previous run still active"
            await asyncio.sleep(3600)
            continue
        _WORKER_LOCKS["periodic_radar"] = True
        _WORKER_STATUS["periodic_radar"]["running"] = True
        start = time.monotonic()
        try:
            # 🌟 自动执行 FrameHdr 每日签到领积分
            try:
                from services.framehdr_service import FrameHdrService
                chk_res = await FrameHdrService.checkin()
                logger.info("FrameHdr daily checkin result: %s", chk_res)
            except Exception as e_chk:
                logger.warning("FrameHdr daily checkin failed: %s", e_chk)

            logger.info("Running scheduled radar sync & scout...")
            await ScoutService.sync_and_scout_all(bot=bot)
            logger.info("Running scheduled hot calendar auto-ingest...")
            await ScoutService.auto_ingest_hot_calendar_shows(bot=bot)
            # Re-warm snapshot cache
            data = await CalendarService.get_today_shows("domestic")
            shows = data.get("shows", [])
            await LibraryService.get_radar_summary(shows, follow_mode="LATEST", force_refresh=True)
            _WORKER_STATUS["periodic_radar"]["last_error"] = None
            _WORKER_STATUS["periodic_radar"]["last_run_seconds"] = time.monotonic() - start
        except Exception as e:
            _WORKER_STATUS["periodic_radar"]["last_error"] = str(e)
            logger.exception("Error in periodic radar worker: %s", e)
        finally:
            _WORKER_LOCKS["periodic_radar"] = False
            _WORKER_STATUS["periodic_radar"]["running"] = False
        # Guangya shares can change without a new Telegram post.
        await asyncio.sleep(600)  # Poll mutable shares every 10 minutes


async def transfer_status_worker(bot: Bot):
    logger.info("ZhuiXin transfer status monitor worker starting...")
    from live_status_monitor import monitor_and_notify_transfer_results
    _WORKER_STATUS["transfer_status"] = {"running": False, "last_error": None, "last_run_seconds": None}
    while True:
        if _WORKER_LOCKS.get("transfer_status"):
            logger.warning("transfer_status worker still running, skipping cycle")
            await asyncio.sleep(25)
            continue
        _WORKER_LOCKS["transfer_status"] = True
        _WORKER_STATUS["transfer_status"]["running"] = True
        start = time.monotonic()
        try:
            await monitor_and_notify_transfer_results(bot)
            _WORKER_STATUS["transfer_status"]["last_error"] = None
            _WORKER_STATUS["transfer_status"]["last_run_seconds"] = time.monotonic() - start
        except Exception as e:
            _WORKER_STATUS["transfer_status"]["last_error"] = str(e)
            logger.error("Error in transfer status worker: %s", e, exc_info=True)
        finally:
            _WORKER_LOCKS["transfer_status"] = False
            _WORKER_STATUS["transfer_status"]["running"] = False
        await asyncio.sleep(25)


async def setup_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="🏠 追新助手全能主菜单"),
        BotCommand(command="hot", description="🔥 热播自动追新入库管理"),
        BotCommand(command="calendar", description="📅 追剧日历 (15大分类导航)"),
        BotCommand(command="radar", description="📡 缺集与追新雷达看板"),
        BotCommand(command="follow", description="📺 我的追更清单管理"),
        BotCommand(command="queue", description="⚡ 查看正在运行与排队的转存任务"),
        BotCommand(command="scan", description="🔍 扫描网盘总目录物理文件"),
        BotCommand(command="sync", description="🔄 立即全库打捞网盘资源"),
        BotCommand(command="help", description="💡 使用说明与指南"),
    ]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
        if ADMIN_TG_ID:
            try:
                await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=ADMIN_TG_ID))
            except Exception:
                pass
        logger.info("ZhuiXinBot: Bot commands successfully registered.")
    except Exception as e:
        logger.warning("Failed to set bot commands: %s", e)


async def main():
    await DatabaseService.init_db()
    from services.cloud_inventory_service import CloudInventoryService
    await CloudInventoryService.init_db()

    # 崩溃告警回调：任何 worker 崩溃都打 ERROR 而不是静默
    def _worker_crash_cb(task: "asyncio.Task"):
        if task.cancelled():
            logger.warning("Worker task %s was cancelled", task.get_name())
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Worker task %s crashed with unhandled exception: %s",
                task.get_name(), exc, exc_info=exc,
            )

    # Auto scan physical cloud disk on boot
    scan_task = asyncio.create_task(
        CloudInventoryService.scan_guangya_master(),
        name="cloud_inventory_scan",
    )
    scan_task.add_done_callback(_worker_crash_cb)

    bot = Bot(token=BOT_TOKEN)
    await setup_commands(bot)

    # 注册全局错误处理器：aiogram handler 抛异常时兜底，避免静默丢消息
    # aiogram 3.x：handler 只收 1 个参数（ErrorEvent），要从中取 update/bot/exception
    async def _global_error_handler(event):
        exc = getattr(event, "exception", None)
        logger.error(
            "Unhandled aiogram exception: %s", exc, exc_info=exc
        )
        # 尝试给用户友好反馈
        try:
            bot_obj = getattr(event, "bot", None)
            update_obj = getattr(event, "update", None)
            msg = getattr(update_obj, "message", None) if update_obj else None
            chat_obj = getattr(msg, "chat", None) if msg else None
            chat_id = getattr(chat_obj, "id", None) if chat_obj else None
            if bot_obj is not None and chat_id is not None:
                await bot_obj.send_message(
                    chat_id,
                    "⚠️ 操作时发生错误，请重试；若持续失败请联系管理员。",
                )
        except Exception:
            pass

    dp.errors.register(_global_error_handler)

    logger.info("ZhuiXin Bot starting polling...")
    ts_task = asyncio.create_task(
        transfer_status_worker(bot), name="transfer_status_monitor"
    )
    ts_task.add_done_callback(_worker_crash_cb)
    radar_task = asyncio.create_task(
        periodic_radar_worker(bot), name="periodic_radar"
    )
    radar_task.add_done_callback(_worker_crash_cb)
    await dp.start_polling(bot)


@dp.message(Command("queue"))
@dp.callback_query(F.data == "menu:queue_status")
async def cb_queue_status(event: types.Message | types.CallbackQuery, bot: Bot):
    if isinstance(event, types.CallbackQuery):
        await event.answer("🔍 正在查询实时转存队列...", show_alert=False)
        msg = event.message
    else:
        msg = event

    import asyncpg, json
    from config import PG_DSN

    try:
        conn = await asyncpg.connect(PG_DSN)
        rows = await conn.fetch("""
            SELECT q.id, q.status, q.locked_by, q.payload, q.next_run_at, q.error_message, q.updated_at,
                   COALESCE(j.title, t.title) as resolved_title
            FROM transfer_queue_tasks q
            LEFT JOIN channel_ingest_jobs j ON j.id = CAST(NULLIF(q.payload->>'job_id', '') AS INTEGER)
            LEFT JOIN tasks t ON t.id = CAST(NULLIF(q.payload->>'task_id', '') AS INTEGER)
            WHERE q.status IN ('RUNNING', 'QUEUED', 'RETRY_WAIT')
            ORDER BY 
                CASE q.status 
                    WHEN 'RUNNING' THEN 1 
                    WHEN 'QUEUED' THEN 2 
                    WHEN 'RETRY_WAIT' THEN 3 
                    ELSE 4 
                END,
                q.id ASC
            LIMIT 15
        """)
        await conn.close()
    except Exception as e:
        logger.exception("Failed to query transfer queue tasks: %s", e)
        err_text = f"❌ 查询转存队列失败：{e}"
        if isinstance(event, types.CallbackQuery):
            await msg.edit_text(err_text, parse_mode="HTML")
        else:
            await msg.answer(err_text, parse_mode="HTML")
        return

    running_tasks = []
    queued_tasks = []
    retry_tasks = []

    for r in rows:
        st = r["status"]
        payload = {}
        try:
            payload = json.loads(r["payload"]) if isinstance(r["payload"], str) else (r["payload"] or {})
        except Exception:
            pass

        title = r.get("resolved_title") or payload.get("title") or payload.get("name") or "未命名任务"
        batch_s = payload.get("transfer_batch", {}).get("season") if isinstance(payload.get("transfer_batch"), dict) else None
        season = batch_s if batch_s is not None else payload.get("season")
        sea_str = f" S{season:02d}" if season else ""
        job_id = payload.get("job_id") or payload.get("transfer_job_id") or r["id"]
        provider = payload.get("provider") or "guangya"
        
        info = {
            "id": r["id"],
            "job_id": job_id,
            "title": title,
            "season_str": sea_str,
            "provider": provider,
            "error": r["error_message"] or "",
        }

        if st == "RUNNING" or (st == "QUEUED" and r.get("locked_by")):
            running_tasks.append(info)
        elif st == "QUEUED":
            queued_tasks.append(info)
        elif st == "RETRY_WAIT":
            retry_tasks.append(info)

    lines = ["⚡ <b>当前转存队列实时运行态看板</b>\n"]

    if running_tasks:
        lines.append("🚀 <b>正在执行中：</b>")
        for t in running_tasks:
            lines.append(f"• <b>《{html.escape(t['title'])}》</b>{t['season_str']} (任务 #{t['id']})")
            lines.append(f"  └ 正在落盘转存/重命名归类，独占处理中...")
        lines.append("")
    else:
        lines.append("💤 <b>当前无正在执行的任务</b>（Worker 空闲待命中）\n")

    if queued_tasks:
        lines.append(f"⏳ <b>排队待转存 ({len(queued_tasks)} 部)：</b>")
        for t in queued_tasks[:5]:
            lines.append(f"• 《{html.escape(t['title'])}》{t['season_str']} (队列 #{t['id']})")
        if len(queued_tasks) > 5:
            lines.append(f"  <i>...及其他 {len(queued_tasks) - 5} 个任务等待中</i>")
        lines.append("")

    if retry_tasks:
        lines.append(f"🔄 <b>等待退避重试 ({len(retry_tasks)} 个)：</b>")
        for t in retry_tasks[:5]:
            err_short = (t["error"][:35] + "...") if len(t["error"]) > 35 else (t["error"] or "等待网络/握手重试")
            lines.append(f"• 《{html.escape(t['title'])}》{t['season_str']}")
            lines.append(f"  └ 原因：<code>{html.escape(err_short)}</code>")
        if len(retry_tasks) > 5:
            lines.append(f"  <i>...及其他 {len(retry_tasks) - 5} 个异常等待中</i>")
        lines.append("")

    lines.append("💡 <i>提示：转存系统采用单 Worker 串行处理以严防网盘 429 风控，任务将按序自动逐部执行完成！</i>")

    text = "\n".join(lines)
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="🔄 刷新队列状态", callback_data="menu:queue_status"),
        types.InlineKeyboardButton(text="🏠 返回主菜单", callback_data="menu:overview"),
    )

    try:
        if isinstance(event, types.CallbackQuery):
            await msg.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        else:
            await msg.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except Exception as exc:
        logger.exception("Telegram queue menu edit failed: %s", exc)
        await msg.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


if __name__ == "__main__":
    asyncio.run(main())

