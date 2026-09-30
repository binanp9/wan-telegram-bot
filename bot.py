#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
from siray import Siray

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("wan-bot")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
SIRAY_API_KEY = os.environ.get("SIRAY_API_KEY", "").strip()
ALLOWED = {
    int(x.strip())
    for x in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

MODEL = os.environ.get("SIRAY_MODEL", "alibaba/wan-3.0-ref2v-spicy")
DEFAULT_SIZE = os.environ.get("DEFAULT_SIZE", "480p")
DEFAULT_ASPECT = os.environ.get("DEFAULT_ASPECT", "16:9")
DEFAULT_DURATION = int(os.environ.get("DEFAULT_DURATION", "5"))

MAX_IMAGES = 10
MAX_VIDEOS = 5
MAX_AUDIOS = 5

if not TELEGRAM_BOT_TOKEN or not SIRAY_API_KEY:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN and SIRAY_API_KEY in the environment.")

siray = Siray(api_key=SIRAY_API_KEY)

# chat_id -> {"images": [...], "videos": [...], "audios": [...]}
refs: dict[int, dict[str, list[str]]] = {}
busy: set[int] = set()


def bucket(chat_id: int) -> dict[str, list[str]]:
    return refs.setdefault(chat_id, {"images": [], "videos": [], "audios": []})


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    if not ALLOWED:
        return True
    return user.id in ALLOWED


async def deny(update: Update) -> None:
    if update.message:
        await update.message.reply_text("This bot is private.")


def counts(chat_id: int) -> str:
    b = bucket(chat_id)
    return (
        f"Images: {len(b['images'])}/{MAX_IMAGES}\n"
        f"Videos: {len(b['videos'])}/{MAX_VIDEOS}\n"
        f"Audio: {len(b['audios'])}/{MAX_AUDIOS}"
    )


def _pick(obj, *names):
    for name in names:
        if isinstance(obj, dict) and obj.get(name) not in (None, ""):
            return obj.get(name)
        val = getattr(obj, name, None)
        if val not in (None, ""):
            return val
    return None


def format_task_failure(status) -> str:
    raw = getattr(status, "raw_response", None) or {}
    data = raw.get("data", raw) if isinstance(raw, dict) else {}
    if not isinstance(data, dict):
        data = {}
    fail_code = _pick(status, "fail_code", "error_code") or _pick(
        data, "fail_code", "error_code", "code"
    )
    fail_reason = _pick(status, "fail_reason", "error", "message") or _pick(
        data, "fail_reason", "error", "message"
    )
    message = _pick(status, "message") or _pick(raw if isinstance(raw, dict) else {}, "message")
    lines = [
        "Generation failed.",
        f"status: {(status.status or 'FAILURE')}",
    ]
    if fail_code:
        lines.append(f"fail_code: {fail_code}")
    if fail_reason:
        lines.append(f"fail_reason: {fail_reason}")
    if message and str(message) not in {str(fail_reason), "OK", "ok"}:
        lines.append(f"message: {message}")
    if not fail_code and not fail_reason:
        lines.append(
            "Siray did not return fail_code/fail_reason. "
            "Check balance, key, and ref duration in the Siray console."
        )
    code_l = f"{fail_code} {fail_reason} {message}".lower()
    if "insufficient" in code_l or "overdue" in code_l or "balance" in code_l:
        lines.append("This looks like a Siray billing/balance problem.")
    try:
        raw_txt = json.dumps(raw, default=str) if raw else ""
        if raw_txt:
            lines.append("raw: " + raw_txt[:1500])
        else:
            lines.append(f"status object: {status!r}"[:1500])
    except Exception:
        lines.append(f"raw: {raw!r}"[:1500])
    return "\n".join(lines)


def format_submit_error(exc: Exception) -> str:
    parts = [f"Submit failed: {exc}"]
    for name in ("code", "error_type", "status_code", "message", "fail_code"):
        val = getattr(exc, name, None)
        if val not in (None, ""):
            parts.append(f"{name}: {val}")
    text = " ".join(parts).lower()
    if "insufficient" in text or "overdue" in text or "balance" in text:
        parts.append("This looks like a Siray billing/balance problem.")
    return "\n".join(parts)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    uid = update.effective_user.id if update.effective_user else "?"
    await update.message.reply_text(
        "Wan 3.0 ref2v bot ready.\n\n"
        f"Your Telegram user id: {uid}\n\n"
        "Send media first (optional), then a prompt:\n"
        "• photos → images[]\n"
        "• videos → videos[]\n"
        "• voice / audio files → audios[]\n\n"
        "/generate a woman walking through neon rain\n"
        "/settings 480p 16:9 5\n"
        "/refs   /clear   /model"
    )


async def show_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    await update.message.reply_text(f"Model: {MODEL}")


async def show_refs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    await update.message.reply_text(counts(update.effective_chat.id))


async def clear_refs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    refs[update.effective_chat.id] = {"images": [], "videos": [], "audios": []}
    await update.message.reply_text("All references cleared.")


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    args = context.args or []
    size = args[0] if len(args) > 0 else context.chat_data.get("size", DEFAULT_SIZE)
    aspect = args[1] if len(args) > 1 else context.chat_data.get("aspect", DEFAULT_ASPECT)
    duration = int(args[2]) if len(args) > 2 else int(
        context.chat_data.get("duration", DEFAULT_DURATION)
    )
    if size not in {"480p", "720p", "1080p"}:
        await update.message.reply_text("Size must be 480p, 720p, or 1080p")
        return
    if aspect not in {"16:9", "9:16", "1:1", "4:3", "3:4", "adaptive"}:
        await update.message.reply_text("Aspect must be 16:9, 9:16, 1:1, 4:3, 3:4, or adaptive")
        return
    if duration < 2 or duration > 30:
        await update.message.reply_text("Duration must be 2–30")
        return
    context.chat_data["size"] = size
    context.chat_data["aspect"] = aspect
    context.chat_data["duration"] = duration
    await update.message.reply_text(f"Saved: {size} · {aspect} · {duration}s")


async def upload_ref(update: Update, kind: str, tg_file, suffix: str, limit: int) -> None:
    chat_id = update.effective_chat.id
    saved = bucket(chat_id)[kind]
    if len(saved) >= limit:
        await update.message.reply_text(f"{kind} full ({limit} max). /clear first.")
        return
    await update.message.reply_text(f"Uploading {kind[:-1]} to Siray…")
    tmp = Path(tempfile.gettempdir()) / f"wan_{chat_id}_{tg_file.file_unique_id}{suffix}"
    try:
        await tg_file.download_to_drive(custom_path=str(tmp))
        url = await asyncio.to_thread(siray.file.upload, str(tmp))
    except Exception as exc:
        log.exception("%s upload failed", kind)
        await update.message.reply_text(f"Upload failed: {exc}")
        return
    finally:
        tmp.unlink(missing_ok=True)
    saved.append(url)
    await update.message.reply_text(f"Saved {kind[:-1]}.\n{counts(chat_id)}")


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    await upload_ref(update, "images", tg_file, ".jpg", MAX_IMAGES)


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    video = update.message.video or update.message.animation or update.message.document
    if video is None:
        return
    tg_file = await video.get_file()
    await upload_ref(update, "videos", tg_file, ".mp4", MAX_VIDEOS)


async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    media = update.message.audio or update.message.voice or update.message.document
    if media is None:
        return
    tg_file = await media.get_file()
    suffix = ".ogg" if update.message.voice else ".mp3"
    await upload_ref(update, "audios", tg_file, suffix, MAX_AUDIOS)


async def lookup_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    if not context.args:
        await update.message.reply_text("Usage: /task TASK_ID")
        return
    task_id = context.args[0].strip()
    try:
        status = await asyncio.to_thread(siray.video.query_task, task_id)
    except Exception as exc:
        await update.message.reply_text(format_submit_error(exc))
        return
    name = (status.status or "").upper()
    if name in {"FAILURE", "FAILED"}:
        await update.message.reply_text(format_task_failure(status))
        return
    raw = getattr(status, "raw_response", None) or {}
    try:
        raw_txt = json.dumps(raw, default=str)[:1500]
    except Exception:
        raw_txt = repr(raw)[:1500]
    await update.message.reply_text(
        f"status: {status.status}\nprogress: {getattr(status, 'progress', None)}\nraw: {raw_txt}"
    )


async def generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    prompt = " ".join(context.args or []).strip()
    if not prompt:
        await update.message.reply_text("Usage: /generate your scene description")
        return
    chat_id = update.effective_chat.id
    if chat_id in busy:
        await update.message.reply_text("Already generating. Wait.")
        return
    size = context.chat_data.get("size", DEFAULT_SIZE)
    aspect = context.chat_data.get("aspect", DEFAULT_ASPECT)
    duration = int(context.chat_data.get("duration", DEFAULT_DURATION))
    b = bucket(chat_id)
    busy.add(chat_id)
    await update.message.reply_text(
        f"Submitting {duration}s {size} {aspect} · audio on\n"
        f"Model: {MODEL}\n{counts(chat_id)}"
    )
    kwargs = {
        "model": MODEL,
        "prompt": prompt,
        "duration": duration,
        "size": size,
        "aspect_ratio": aspect,
        "prompt_expansion_enable": True,
        "audio_enable": True,
    }
    if b["images"]:
        kwargs["images"] = b["images"][:MAX_IMAGES]
    if b["videos"]:
        kwargs["videos"] = b["videos"][:MAX_VIDEOS]
    if b["audios"]:
        kwargs["audios"] = b["audios"][:MAX_AUDIOS]
    try:
        response = await asyncio.to_thread(siray.video.generate_async, **kwargs)
        task_id = response.task_id
    except Exception as exc:
        busy.discard(chat_id)
        log.exception("submit failed")
        await update.message.reply_text(format_submit_error(exc))
        return
    await update.message.reply_text(f"Queued.\nTask: {task_id}")
    context.application.create_task(poll_and_send(context.application, chat_id, task_id))


async def poll_and_send(app, chat_id: int, task_id: str) -> None:
    try:
        for _ in range(240):
            await asyncio.sleep(5)
            try:
                status = await asyncio.to_thread(siray.video.query_task, task_id)
            except Exception as exc:
                log.warning("poll error: %s", exc)
                continue
            name = (status.status or "").upper()
            if name == "SUCCESS":
                urls = status.outputs or []
                if not urls:
                    await app.bot.send_message(chat_id, "Done, but no file URL.")
                    return
                url = urls[0]
                try:
                    await app.bot.send_video(chat_id, video=url, caption="Done.")
                except Exception:
                    await app.bot.send_message(chat_id, f"Done:\n{url}")
                return
            if name in {"FAILURE", "FAILED"}:
                await app.bot.send_message(chat_id, format_task_failure(status))
                return
        await app.bot.send_message(chat_id, f"Timed out waiting on {task_id}")
    finally:
        busy.discard(chat_id)


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("model", show_model))
    app.add_handler(CommandHandler("refs", show_refs))
    app.add_handler(CommandHandler("clear", clear_refs))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("generate", generate))
    app.add_handler(CommandHandler("task", lookup_task))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(
        MessageHandler(filters.VIDEO | filters.ANIMATION | filters.Document.VIDEO, on_video)
    )
    app.add_handler(
        MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.AUDIO, on_audio)
    )
    log.info("Bot polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
