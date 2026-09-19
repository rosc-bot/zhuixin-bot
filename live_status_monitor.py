import asyncio
import asyncpg
import aiohttp
import json
import logging
import sqlite3
from html import escape
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone, timedelta

from aiogram import types
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import ADMIN_TG_ID, LOCAL_DB_PATH, INGEST_PUSH_TOKEN, PG_DSN

logger = logging.getLogger(__name__)
STALE_FAILURE_MINUTES = 45


def format_failure_reason(error_message: Optional[str]) -> Dict[str, str]:
    raw = str(error_message or "").strip()
    if not raw or raw.lower() in ("none", "null"):
        return {
            "summary": "未捕获到具体错误详情",
            "detail": "网盘转存 Worker 未记录详细错误堆栈，可能为内部超时或中断退出。",
            "solution": "建议直接点击【🔄 重试转存】再次尝试；若仍失败可尝试【🔍 重新打捞】新资源。",
            "raw": "None",
        }

    raw_lower = raw.lower()

    if "180 秒内仅确认 0" in raw or "确认 0 个转存文件" in raw:
        return {
            "summary": "网盘转存确认超时",
            "detail": "网盘服务端排队严重或该分享包含的文件体量过大，在规定超时窗口内未完成落盘确认。",
            "solution": "网盘服务端可能仍在后台排队，建议点击【🔄 重试转存】继续处理，或点击【🔍 重新打捞】。",
            "raw": raw,
        }
    if "tmdb" in raw_lower or "cannot access local variable 'tmdb_info'" in raw_lower or "tmdb_id" in raw_lower:
        return {
            "summary": "TMDB 元数据匹配异常",
            "detail": "无法从 TMDB 获取该剧的标准剧名、分季或上映排期，导致目录对齐失败。",
            "solution": "建议检查剧名是否有特殊符号，或点击【🔄 重试转存】重新获取元数据。",
            "raw": raw,
        }
    if any(k in raw_lower for k in ("404", "expired", "deleted", "share not found", "不存在", "失效", "取消分享")):
        return {
            "summary": "源网盘分享链接已失效",
            "detail": "源发布者已取消该分享，或者该资源已被网盘平台封禁/屏蔽下架。",
            "solution": "当前链接已不可用，建议直接点击下方【🔍 重新打捞】在各频道中重新检索其他替代分享。",
            "raw": raw,
        }
    if any(k in raw_lower for k in ("quota", "full", "容量不足", "空间不足", "storage limit")):
        return {
            "summary": "目标网盘存储空间不足",
            "detail": "目标网盘当前可用容量不足以容纳该批次转存影视文件。",
            "solution": "请先清理网盘容量或扩容，随后点击【🔄 重试转存】。",
            "raw": raw,
        }
    if any(k in raw_lower for k in ("cookie", "token", "unauthorized", "401", "403", "forbidden", "未登录")):
        return {
            "summary": "网盘授权或凭证失效",
            "detail": "用于转存的网盘账号 Cookie 或 OAuth Token 已过期，导致无法调用转存接口。",
            "solution": "请在转存机器人后台刷新网盘账号授权，刷新后点击【🔄 重试转存】。",
            "raw": raw,
        }
    if any(k in raw_lower for k in ("rate limit", "429", "too many requests", "频率限制", "频繁")):
        return {
            "summary": "触发网盘 API 频率限制",
            "detail": "短时间内向网盘提交过多转存请求，触发网盘官方限流风控。",
            "solution": "系统或账号正受频率保护，建议稍等几分钟后点击【🔄 重试转存】。",
            "raw": raw,
        }
    if any(k in raw_lower for k in ("connect", "timeout", "timed out", "econnrefused", "clientconnectorerror", "network")):
        return {
            "summary": "网络连接超时或网盘服务无响应",
            "detail": "与网盘服务端通信超时或网络握手失败，未能建立稳定连接。",
            "solution": "通常为偶发网络抖动，建议点击【🔄 重试转存】即可恢复。",
            "raw": raw,
        }
    if "无法匹配" in raw or "未能识别" in raw or "unrecognized" in raw_lower:
        return {
            "summary": "影视剧名或季集无法智能对齐",
            "detail": "分享中的文件名格式非标准，系统无法准确解析对应第几季第几集。",
            "solution": "建议在转存面板手动指定识别，或点击【🔍 重新打捞】寻找规范命名的资源。",
            "raw": raw,
        }

    clean_raw = raw[:300]
    return {
        "summary": "转存队列执行异常",
        "detail": f"后台转存 Worker 处理任务时返回异常中断：{clean_raw}",
        "solution": "可优先点击下方【🔄 重试转存】；若该分享多次失败，建议点击【🔍 重新打捞】。",
        "raw": raw,
    }


async def retry_transfer_job(job_id: int) -> Dict[str, Any]:
    try:
        conn = await asyncpg.connect(PG_DSN)
        row = await conn.fetchrow("""
            SELECT id, status, transfer_status, error_message, channel_id, message_id,
                   provider, share_url, title, media_type, year, season, tmdb_id, parsed_data
            FROM channel_ingest_jobs
            WHERE id = $1
        """, job_id)

        if not row:
            await conn.close()
            return {"success": False, "error": f"找不到任务 #{job_id}"}

        tasks = await conn.fetch("""
            SELECT id, status, payload, attempt_count
            FROM transfer_queue_tasks
            WHERE (payload ->> 'job_id') = $1
            ORDER BY id DESC
        """, str(job_id))

        if tasks:
            task_id = tasks[0]["id"]
            await conn.execute("""
                UPDATE transfer_queue_tasks
                SET status = 'QUEUED',
                    next_run_at = NOW(),
                    locked_at = NULL,
                    locked_by = NULL,
                    error_message = NULL,
                    failure_notification_sent_at = NULL,
                    attempt_count = 0
                WHERE id = $1
            """, task_id)
            logger.info("Requeued TransferQueueTask #%s for job #%s", task_id, job_id)
        else:
            payload = {
                "job_id": job_id,
                "channel_id": row["channel_id"] or "-1003961136374",
                "message_id": row["message_id"] or 0,
                "provider": row["provider"] or "guangya",
                "share_url": row["share_url"] or "",
                "title": row["title"],
                "media_type": row["media_type"] or "TV",
                "year": row["year"],
                "season": row["season"],
                "seasons": [row["season"]] if row["season"] else [],
                "season_range": str(row["season"]) if row["season"] else None,
                "season_episodes": None,
                "tmdb_id": row["tmdb_id"],
                "transfer_source": "AUTO_CHANNEL",
            }
            await conn.execute("""
                INSERT INTO transfer_queue_tasks (
                    status, priority, attempt_count, max_retries,
                    next_run_at, payload, created_at, updated_at
                ) VALUES (
                    'QUEUED', 100, 0, 3,
                    NOW(), $1, NOW(), NOW()
                )
            """, json.dumps(payload))
            logger.info("Created new TransferQueueTask for job #%s", job_id)

        await conn.execute("""
            UPDATE channel_ingest_jobs
            SET status = 'READY',
                transfer_status = 'QUEUED',
                error_message = NULL,
                updated_at = NOW()
            WHERE id = $1
        """, job_id)

        await conn.close()

        conn_sq = sqlite3.connect(LOCAL_DB_PATH)
        conn_sq.execute("DELETE FROM notified_ingest_jobs WHERE job_id = ?", (job_id,))
        conn_sq.commit()
        conn_sq.close()

        return {"success": True, "job_id": job_id}
    except Exception as e:
        logger.exception("Failed to retry transfer job %d: %s", job_id, e)
        return {"success": False, "error": str(e)}


async def cancel_transfer_job(job_id: int, reason: str = "管理员手动取消") -> Dict[str, Any]:
    try:
        conn = await asyncpg.connect(PG_DSN)
        await conn.execute("""
            UPDATE transfer_queue_tasks
            SET status = 'CANCELLED',
                locked_at = NULL,
                locked_by = NULL,
                error_message = $1,
                updated_at = NOW()
            WHERE (payload ->> 'job_id') = $2
              AND status IN ('QUEUED', 'RUNNING', 'RETRY_WAIT', 'FAILED')
        """, f"已取消: {reason}", str(job_id))

        await conn.execute("""
            UPDATE channel_ingest_jobs
            SET status = 'CANCELLED',
                transfer_status = 'CANCELLED',
                error_message = $1,
                updated_at = NOW()
            WHERE id = $2
        """, f"管理员取消: {reason}", job_id)

        await conn.close()

        conn_sq = sqlite3.connect(LOCAL_DB_PATH)
        conn_sq.execute("""
            INSERT OR REPLACE INTO notified_ingest_jobs (job_id, status, failure_key)
            VALUES (?, 'IGNORED', 'MANUAL_CANCELLED')
        """, (job_id,))
        conn_sq.commit()
        conn_sq.close()

        return {"success": True, "job_id": job_id}
    except Exception as e:
        logger.exception("Failed to cancel transfer job %d: %s", job_id, e)
        return {"success": False, "error": str(e)}


async def get_job_info(job_id: int) -> Optional[Dict[str, Any]]:
    try:
        conn = await asyncpg.connect(PG_DSN)
        row = await conn.fetchrow("""
            SELECT id, title, season, share_url, provider, status, transfer_status, error_message, parsed_data
            FROM channel_ingest_jobs
            WHERE id = $1
        """, job_id)
        await conn.close()
        if not row:
            return None
        parsed = {}
        if row["parsed_data"]:
            try:
                parsed = json.loads(row["parsed_data"]) if isinstance(row["parsed_data"], str) else row["parsed_data"]
            except Exception:
                pass
        return {
            "id": row["id"],
            "title": row["title"] or parsed.get("share_title") or "未命名剧集",
            "season": row["season"] or 1,
            "share_url": row["share_url"],
            "provider": row["provider"],
            "status": row["status"],
            "transfer_status": row["transfer_status"],
            "error_message": row["error_message"],
        }
    except Exception as e:
        logger.warning("Failed to get job info %d: %s", job_id, e)
        return None


async def retry_failed_scout_push(push_id: int, bot: Optional[Any] = None) -> Dict[str, Any]:
    """重试失败的追新打捞推送。任何异常都被捕获，绝不静默卡住按钮。"""
    from database import DatabaseService
    try:
        row = await DatabaseService.get_failed_scout_push(push_id)
        if not row:
            return {"success": False, "error": f"找不到推送记录 #{push_id}"}

        title = row["title"]
        season = row["season"]
        episodes = row.get("episodes") or []
        share_url = row.get("share_url") or ""
        text_context = row.get("text_context") or ""

        from services.scout_service import ScoutService
        push_res = await ScoutService.push_to_tg_media_bot(
            title=title,
            season=season,
            episodes=episodes,
            share_url=share_url,
            text_context=text_context,
        )

        if push_res.get("success"):
            try:
                await DatabaseService.update_failed_scout_push_status(push_id, "REQUEUED")
            except Exception as upd_err:
                logger.error("Retry succeeded but failed to update status for push #%d: %s",
                             push_id, upd_err)
            return {"success": True, "data": push_res.get("data")}
        else:
            err = push_res.get("error") or "推送失败"
            try:
                await DatabaseService.update_failed_scout_push_status(
                    push_id, "FAILED", error_message=err
                )
            except Exception as upd_err:
                logger.error("Retry failed and also failed to record error for push #%d: %s",
                             push_id, upd_err)
            return {"success": False, "error": err}
    except Exception as e:
        logger.exception("retry_failed_scout_push raised for push #%d: %s", push_id, e)
        return {"success": False, "error": f"重试过程异常：{e}"}


async def cancel_failed_scout_push(push_id: int) -> Dict[str, Any]:
    from database import DatabaseService
    try:
        row = await DatabaseService.get_failed_scout_push(push_id)
        if not row:
            return {"success": False, "error": f"找不到推送记录 #{push_id}"}
        await DatabaseService.update_failed_scout_push_status(push_id, "CANCELLED")
        return {"success": True, "push_id": push_id}
    except Exception as e:
        logger.exception("cancel_failed_scout_push raised for push #%d: %s", push_id, e)
        return {"success": False, "error": f"取消过程异常：{e}"}


def _effective_status(transfer_status: Any, status: Any) -> str:
    """Prefer a meaningful transfer status; treat NONE/NULL as absent."""
    for raw in (transfer_status, status):
        value = str(raw or "").strip().upper()
        if value and value not in {"NONE", "NULL"}:
            return value
    return ""


def _age_minutes(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 60)
    except (TypeError, ValueError, OverflowError):
        return None


NOTIFICATION_CLAIM_MINUTES = 5


def _claim_failure_notification(conn: sqlite3.Connection, job_id: int, failure_key: str) -> bool:
    """Atomically claim one failure event before sending Telegram."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, failure_key, notified_at FROM notified_ingest_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row:
            status, recorded_key, notified_at = row
            if recorded_key == failure_key and status in {
                "FAILED", "HISTORICAL", "IGNORED", "IGNORED_DUPLICATE"
            }:
                conn.rollback()
                return False
            if recorded_key == failure_key and status == "SENDING":
                age = _age_minutes(notified_at)
                if age is not None and age < NOTIFICATION_CLAIM_MINUTES:
                    conn.rollback()
                    return False
        conn.execute(
            "INSERT OR REPLACE INTO notified_ingest_jobs (job_id, status, failure_key) VALUES (?, ?, ?)",
            (job_id, "SENDING", failure_key),
        )
        conn.commit()
        return True
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        logger.warning("Could not claim watchlist failure notification job=%s: %s", job_id, exc)
        return False


def _failure_key(row: Any) -> str:
    """Identify one failure event, not merely one logical job."""
    stamp = row["updated_at"] or row["created_at"]
    if isinstance(stamp, datetime):
        stamp = stamp.astimezone(timezone.utc).isoformat()
    return f"{stamp or ''}|{row['error_message'] or ''}"


async def get_live_ingest_history(limit: int = 15) -> List[Dict[str, Any]]:
    """Fetch live ChannelIngestJob records initiated by watchlist/scout with real status and errors."""
    try:
        conn = await asyncpg.connect(PG_DSN)
        rows = await conn.fetch("""
            SELECT id, title, season, share_url, provider, status, transfer_status, error_message, created_at, updated_at, parsed_data
            FROM channel_ingest_jobs
            WHERE source_type = 'watchlist_scout'
               OR (parsed_data ->> 'watchlist_incremental_active') = 'true'
            ORDER BY id DESC
            LIMIT $1
        """, limit)
        await conn.close()
    except Exception as e:
        logger.warning("Failed to fetch live ingest history: %s", e)
        return []

    cst = timezone(timedelta(hours=8))
    results = []
    for r in rows:
        title = r["title"]
        parsed = {}
        if r["parsed_data"]:
            try:
                parsed = json.loads(r["parsed_data"]) if isinstance(r["parsed_data"], str) else r["parsed_data"]
            except Exception:
                pass
        if not title:
            title = parsed.get("share_title") or "未命名作品"
        
        st = _effective_status(r["transfer_status"], r["status"])
        t_str = ""
        if r["created_at"]:
            try:
                dt = r["created_at"].astimezone(cst)
                t_str = dt.strftime("%m-%d %H:%M")
            except Exception:
                t_str = str(r["created_at"])[:16]

        results.append({
            "id": r["id"],
            "title": title,
            "season": r["season"] or 1,
            "provider": r["provider"] or "guangya",
            "share_url": r["share_url"],
            "status": st,
            "error_message": r["error_message"],
            "created_at_cst": t_str,
            "episodes": parsed.get("transfer_candidates") or parsed.get("detected_episodes") or [],
        })
    return results


async def monitor_and_notify_transfer_results(bot: Any) -> None:
    """
    Periodically check recent watchlist_scout jobs and ONLY notify admin if a transfer FAILS.
    Rule:
      1. ONLY monitors jobs where source_type = 'watchlist_scout' (strictly separated from publish/manual tasks).
      2. NEVER notifies on SUCCESS (the media transfer channel handles successful releases).
      3. ONLY notifies on FAILED status, with clear root cause, source link, and action buttons.
    """
    if not bot or not ADMIN_TG_ID:
        return

    conn_sq = sqlite3.connect(LOCAL_DB_PATH)
    c_sq = conn_sq.cursor()
    c_sq.execute("""
        CREATE TABLE IF NOT EXISTS notified_ingest_jobs (
            job_id INTEGER PRIMARY KEY,
            status TEXT,
            notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn_sq.commit()
    columns = {row[1] for row in c_sq.execute("PRAGMA table_info(notified_ingest_jobs)")}
    if "failure_key" not in columns:
        c_sq.execute("ALTER TABLE notified_ingest_jobs ADD COLUMN failure_key TEXT")
        conn_sq.commit()

    try:
        conn = await asyncpg.connect(PG_DSN)
        rows = await conn.fetch("""
            SELECT id, title, season, share_url, provider, status, transfer_status, error_message, created_at, updated_at, parsed_data
            FROM channel_ingest_jobs
            WHERE source_type = 'watchlist_scout'
               OR (parsed_data ->> 'watchlist_incremental_active') = 'true'
            ORDER BY id DESC
            LIMIT 100
        """)
        await conn.close()
    except Exception as e:
        logger.warning("Error fetching jobs for status monitor: %s", e)
        conn_sq.close()
        return

    for r in rows:
        job_id = r["id"]
        row_notified = c_sq.execute(
            "SELECT status, failure_key, notified_at FROM notified_ingest_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        raw_transfer_status = str(r["transfer_status"] or "").strip().upper()
        if raw_transfer_status != "FAILED":
            if row_notified and row_notified[0] in {"FAILED", "HISTORICAL"}:
                c_sq.execute(
                    "UPDATE notified_ingest_jobs SET status = ? WHERE job_id = ?",
                    ("CLEARED", job_id),
                )
                conn_sq.commit()
            continue

        title = r["title"]
        parsed = {}
        if r["parsed_data"]:
            try:
                parsed = json.loads(r["parsed_data"]) if isinstance(r["parsed_data"], str) else r["parsed_data"]
            except Exception:
                pass
        if not title:
            title = parsed.get("share_title") or "未命名作品"
        sea = r["season"] or 1
        prov = r["provider"] or "guangya"
        share_u = r["share_url"] or ""
        err = r["error_message"] or ""
        failure_key = _failure_key(r)
        if row_notified and row_notified[1] == failure_key and row_notified[0] in {
            "FAILED", "HISTORICAL", "IGNORED", "IGNORED_DUPLICATE"
        }:
            continue
        error_lower = err.lower()
        non_failure_markers = (
            "已接受 resource",
            "相同频道转存键",
            "resource 占用",
            "另一个进程处理",
            "duplicate",
        )
        if any(marker in error_lower for marker in non_failure_markers):
            c_sq.execute(
                "INSERT OR REPLACE INTO notified_ingest_jobs (job_id, status, failure_key) VALUES (?, ?, ?)",
                (job_id, "IGNORED_DUPLICATE", failure_key),
            )
            conn_sq.commit()
            logger.info("Skipped non-failure duplicate/concurrency result for watchlist job %d", job_id)
            continue

        if "管理员取消" in err:
            c_sq.execute(
                "INSERT OR REPLACE INTO notified_ingest_jobs (job_id, status, failure_key) VALUES (?, ?, ?)",
                (job_id, "IGNORED", failure_key),
            )
            conn_sq.commit()
            continue

        age_minutes = _age_minutes(r["updated_at"] or r["created_at"])
        if row_notified and row_notified[0] == "FAILED" and row_notified[1] == failure_key:
            continue
        if age_minutes is None or age_minutes > STALE_FAILURE_MINUTES:
            c_sq.execute(
                "INSERT OR REPLACE INTO notified_ingest_jobs (job_id, status, failure_key) VALUES (?, ?, ?)",
                (job_id, "HISTORICAL", failure_key),
            )
            conn_sq.commit()
            logger.info("Skipped stale watchlist failure job %d (age=%.1f min)", job_id, age_minutes or -1)
            continue

        if not _claim_failure_notification(conn_sq, int(job_id), failure_key):
            logger.info("Skipped already-claimed watchlist failure job %d", job_id)
            continue

        f_info = format_failure_reason(err)

        safe_title = escape(str(title))
        safe_prov = escape(str(prov))
        safe_sum = escape(f_info["summary"])
        safe_detail = escape(f_info["detail"])
        safe_sol = escape(f_info["solution"])
        safe_raw = escape(f_info["raw"])
        safe_share_u = escape(str(share_u))

        builder = InlineKeyboardBuilder()
        builder.row(
            types.InlineKeyboardButton(text="🔄 重试转存", callback_data=f"tx_fail_retry:{job_id}"),
            types.InlineKeyboardButton(text="🛑 取消转存", callback_data=f"tx_fail_cancel:{job_id}"),
        )
        builder.row(
            types.InlineKeyboardButton(text="🔍 重新打捞", callback_data=f"tx_fail_rescout:{job_id}"),
            types.InlineKeyboardButton(text="🚫 忽略此剧", callback_data=f"tx_fail_ignore:{job_id}"),
        )

        msg_text = (
            "⚠️ <b>追新影视转存失败告警</b>\n\n"
            f"🎬 <b>剧名：</b>《{safe_title}》 第 {sea} 季\n"
            f"💾 <b>网盘：</b><code>{safe_prov}</code>\n"
            f"📌 <b>任务编号：</b>Job <code>#{job_id}</code>\n\n"
            f"❌ <b>失败原因分析：</b>\n"
            f"• <b>原因归类：</b>{safe_sum}\n"
            f"• <b>具体排查：</b>{safe_detail}\n"
            f"• <b>处置建议：</b>{safe_sol}\n"
            f"• <b>技术详情：</b><code>{safe_raw}</code>\n\n"
            f"🔗 <b>源链接：</b><code>{safe_share_u}</code>\n\n"
            "💡 您可以直接点击下方快捷按钮进行一键重试、取消任务或重新打捞！"
        )

        try:
            logger.info("Skip zhuixin-bot push for job %s (unified to tg-media-bot)", job_id)
            c_sq.execute(
                "INSERT OR REPLACE INTO notified_ingest_jobs (job_id, status, failure_key) VALUES (?, ?, ?)",
                (job_id, "FAILED", failure_key)
            )
            conn_sq.commit()
            logger.info("Sent transfer failure alert with buttons for watchlist job %d (%s)", job_id, title)
        except Exception as se:
            logger.warning("Failed to send transfer notification: %s", se)

    conn_sq.close()
