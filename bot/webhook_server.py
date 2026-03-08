"""
Webhook server — receives CloudConvert job events and forwards them to the bot.
Runs as a FastAPI app on port 8080 alongside the Telegram bot.
"""

import asyncio
import hashlib
import hmac
import logging
import os

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

app = FastAPI()

# Will be set by main.py after bot starts
_bot_client   = None
_admin_id     = None
_webhook_secret = None  # optional CloudConvert signing secret

# Active job tracking: { job_id: { "msg_id": int, "chat_id": int, "status": str } }
ACTIVE_JOBS: dict[str, dict] = {}


def init(bot_client, admin_id: int, webhook_secret: str = ""):
    global _bot_client, _admin_id, _webhook_secret
    _bot_client     = bot_client
    _admin_id       = admin_id
    _webhook_secret = webhook_secret


@app.get("/")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def cloudconvert_webhook(request: Request):
    body = await request.body()

    # Verify signature if secret is set
    if _webhook_secret:
        sig = request.headers.get("CloudConvert-Signature", "")
        expected = hmac.new(_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(status_code=401, detail="Invalid signature")

    data  = await request.json()
    event = data.get("event", "")
    job   = data.get("job", {})
    task  = data.get("task", {})

    # Use job id or task id as key
    job_id = job.get("id") or task.get("job_id") or task.get("id", "")

    asyncio.create_task(_handle_event(event, job_id, job, task))
    return JSONResponse({"ok": True})


async def _handle_event(event: str, job_id: str, job: dict, task: dict):
    if not _bot_client or not _admin_id:
        return

    status  = job.get("status") or task.get("status", "")
    tasks   = job.get("tasks", [])

    # Progress bar helper
    def _bar(pct: int) -> str:
        filled = pct // 5
        return "█" * filled + "░" * (20 - filled)

    try:
        if event == "job.created":
            # Send initial message and store msg_id
            msg = await _bot_client.send_message(
                chat_id=_admin_id,
                text=(
                    f"☁️ <b>CloudConvert Job Started</b>\n\n"
                    f"🆔 <code>{job_id}</code>\n\n"
                    f"<code>{_bar(0)}</code> <b>0%</b>\n"
                    f"⏳ <i>Waiting…</i>"
                )
            )
            ACTIVE_JOBS[job_id] = {
                "chat_id": _admin_id,
                "msg_id":  msg.id,
                "status":  "created",
            }

        elif event in ("job.updated", "task.updated"):
            entry = ACTIVE_JOBS.get(job_id)

            # Count task progress
            total_tasks = len(tasks) or 1
            done_tasks  = sum(1 for t in tasks if t.get("status") == "finished")
            pct         = int(done_tasks * 100 / total_tasks)

            # Current active task name
            active = next(
                (t.get("operation", "") for t in tasks if t.get("status") == "processing"),
                status
            )

            if entry:
                try:
                    await _bot_client.edit_message_text(
                        chat_id=entry["chat_id"],
                        message_id=entry["msg_id"],
                        text=(
                            f"☁️ <b>CloudConvert</b>\n\n"
                            f"🆔 <code>{job_id}</code>\n\n"
                            f"<code>{_bar(pct)}</code> <b>{pct}%</b>\n"
                            f"⚙️ <code>{active}</code>"
                        )
                    )
                except Exception:
                    pass

        elif event == "job.finished":
            entry = ACTIVE_JOBS.pop(job_id, {})

            # Find download URL from export task
            download_url = ""
            for t in tasks:
                files = t.get("result", {}).get("files", [])
                if files:
                    download_url = files[0].get("url", "")
                    break

            if not download_url:
                if entry:
                    await _bot_client.edit_message_text(
                        chat_id=entry["chat_id"],
                        message_id=entry["msg_id"],
                        text=f"☁️ <b>Job finished</b> but no download URL found.\n🆔 <code>{job_id}</code>"
                    )
                return

            # Update message to downloading state
            chat_id = entry.get("chat_id", _admin_id)
            if entry:
                try:
                    await _bot_client.edit_message_text(
                        chat_id=chat_id,
                        message_id=entry["msg_id"],
                        text=(
                            f"☁️ <b>Job Done!</b>\n\n"
                            f"🆔 <code>{job_id}</code>\n\n"
                            f"<code>{'█' * 20}</code> <b>100%</b>\n"
                            f"📥 <i>Downloading…</i>"
                        )
                    )
                except Exception:
                    pass

            # Download and send
            import uuid
            from processors.leech import direct_download
            from utils.file_utils import cleanup
            from utils.queue import register, update_status, finish

            dl_job_id = str(uuid.uuid4())[:8]
            register(dl_job_id, _admin_id, "cloudconvert", "cloudconvert", job_id[:30])

            # Create a fake progress msg proxy
            class _MsgProxy:
                def __init__(self, client, cid, mid):
                    self._c, self._cid, self._mid = client, cid, mid
                async def edit(self, text):
                    try:
                        await self._c.edit_message_text(self._cid, self._mid, text)
                    except Exception:
                        pass

            proxy = _MsgProxy(_bot_client, chat_id, entry.get("msg_id", 0)) if entry else None
            path  = None

            try:
                update_status(dl_job_id, "📥 Downloading…")
                path = await direct_download(download_url, dl_job_id, progress_msg=proxy)

                # Upload to Telegram
                from pathlib import Path
                import os as _os
                file_name = Path(path).name
                ext       = Path(path).suffix.lower()
                VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v"}

                if ext in VIDEO_EXTS:
                    await _bot_client.send_video(
                        chat_id=chat_id,
                        video=path,
                        caption=f"✅ <b>Done</b> — <code>{job_id}</code>",
                        file_name=file_name,
                        supports_streaming=True,
                    )
                else:
                    await _bot_client.send_document(
                        chat_id=chat_id,
                        document=path,
                        caption=f"✅ <b>Done</b> — <code>{job_id}</code>",
                        file_name=file_name,
                    )

                # Delete progress message
                if entry:
                    try:
                        await _bot_client.delete_messages(chat_id, entry["msg_id"])
                    except Exception:
                        pass

            except Exception as e:
                logger.error(f"CloudConvert download failed: {e}", exc_info=True)
                await _bot_client.send_message(
                    chat_id=chat_id,
                    text=f"❌ <b>Download failed</b>\n\n<code>{str(e)[:300]}</code>"
                )
            finally:
                finish(dl_job_id)
                cleanup(path)

        elif event == "job.failed":
            entry = ACTIVE_JOBS.pop(job_id, {})
            err   = job.get("tasks", [{}])
            err_msg = next((t.get("message", "") for t in err if t.get("status") == "error"), "Unknown error")
            chat_id = entry.get("chat_id", _admin_id)
            mid     = entry.get("msg_id")
            text    = f"❌ <b>CloudConvert job failed</b>\n\n🆔 <code>{job_id}</code>\n<code>{err_msg[:200]}</code>"
            if mid:
                try:
                    await _bot_client.edit_message_text(chat_id, mid, text)
                except Exception:
                    await _bot_client.send_message(chat_id, text)
            else:
                await _bot_client.send_message(chat_id, text)

    except Exception as e:
        logger.error(f"Webhook handler error: {e}", exc_info=True)
