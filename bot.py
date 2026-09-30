#!/usr/bin/env python3
"""Telegram bot that orders Alibaba Wan 3.0 videos through the Siray API."""

from __future__ import annotations

import asyncio
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

if not TELEGRAM_BOT_TOKEN or not SIRAY_API_KEY:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN and SIRAY_API_KEY in the environment.")

siray = Siray(api_key=SIRAY_API_KEY)

# chat_id -> list of Siray-hosted HTTPS URLs
refs: dict[int, list[str]] = {}
busy: set[int] = set()


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    uid = update.effective_user.id if update.effective_user else "?"
    await update.message.reply_text(
        "Wan 3.0 bot ready.\n\n"
        f"Your Telegram user id: {uid}\n"
        "Save that number if you need ALLOWED_USER_IDS.\n\n"
        "1. Send 1–10 reference photos (optional)\n"
        "2. /generate a woman walking through neon rain\n\n"
        "Other commands:\n"
        "/settings 480p 16:9 5\n"
        "/refs — how many photos saved\n"
        "/clear — drop photos\n"
        "/model — which Wan endpoint is active"
    )


async def show_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    await update.message.reply_text(f"Model: {MODEL}")


async def show_refs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    n = len(refs.get(update.effective_chat.id, []))
    await update.message.reply_text(f"{n} reference file(s) saved.")


async def clear_refs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    refs[update.effective_chat.id] = []
    await update.message.reply_text("References cleared.")


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    args = context.args or []
    size = args[0] if len(args) > 0 else context.chat_data.get("size", DEFAULT_SIZE)
    aspect = args[1] if len(args) > 1 else context.chat_data.get("aspect", DEFAULT_ASPECT)
    duration = int(args[2]) if len(args) > 2 else int(context.chat_data.get("duration", DEFAULT_DURATION))
    if size not in {"480p", "720p", "1080p"}:
        await update.message.reply_text("Size must be 480p, 720p, or 1080p")
        return
    if aspect not in {"16:9", "9:16", "1:1", "4:3", "3:4", "adaptive"}:
        await update.message.reply_text("Aspect must be 16:9, 9:16, 1:1, 4:3, 3:4, or adaptive")
        return
    if duration < 2 or duration > 30:
        await update.message.reply_text("Duration must be 2–30 seconds")
        return
    context.chat_data["size"] = size
    context.chat_data["aspect"] = aspect
    context.chat_data["duration"] = duration
    await update.message.reply_text(f"Saved: {size} · {aspect} · {duration}s")


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    chat_id = update.effective_chat.id
    saved = refs.setdefault(chat_id, [])
    if len(saved) >= 10:
        await update.message.reply_text("Already have 10 references. /clear first.")
        return

    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    await update.message.reply_text("Uploading reference to Siray…")

    tmp = Path(tempfile.gettempdir()) / f"wan_{chat_id}_{photo.file_unique_id}.jpg"
    try:
        await tg_file.download_to_drive(custom_path=str(tmp))
        url = await asyncio.to_thread(siray.file.upload, str(tmp))
    except Exception as exc:
        log.exception("photo upload failed")
        await update.message.reply_text(f"Upload failed: {exc}")
        return
    finally:
        tmp.unlink(missing_ok=True)

    saved.append(url)
    await update.message.reply_text(f"Saved reference {len(saved)}/10")


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    chat_id = update.effective_chat.id
    saved = refs.setdefault(chat_id, [])
    if len(saved) >= 10:
        await update.message.reply_text("Already have 10 references. /clear first.")
        return

    video = update.message.video or update.message.document
    if video is None:
        return
    tg_file = await video.get_file()
    await update.message.reply_text("Uploading video reference to Siray…")

    suffix = ".mp4"
    tmp = Path(tempfile.gettempdir()) / f"wan_{chat_id}_{video.file_unique_id}{suffix}"
    try:
        await tg_file.download_to_drive(custom_path=str(tmp))
        url = await asyncio.to_thread(siray.file.upload, str(tmp))
    except Exception as exc:
        log.exception("video upload failed")
        await update.message.reply_text(f"Upload failed: {exc}")
        return
    finally:
        tmp.unlink(missing_ok=True)

    saved.append(url)
    await update.message.reply_text(f"Saved reference {len(saved)}/10 (includes video)")


async def generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)

    prompt = " ".join(context.args or []).strip()
    if not prompt:
        await update.message.reply_text("Usage: /generate your scene description")
        return

    chat_id = update.effective_chat.id
    if chat_id in busy:
        await update.message.reply_text("Already generating. Wait for that one to finish.")
        return

    size = context.chat_data.get("size", DEFAULT_SIZE)
    aspect = context.chat_data.get("aspect", DEFAULT_ASPECT)
    duration = int(context.chat_data.get("duration", DEFAULT_DURATION))
    images = list(refs.get(chat_id, []))

    busy.add(chat_id)
    await update.message.reply_text(
        f"Submitting {duration}s {size} {aspect}\nModel: {MODEL}\nRefs: {len(images)}"
    )

    kwargs = {
        "model": MODEL,
        "prompt": prompt,
        "duration": duration,
        "size": size,
        "aspect_ratio": aspect,
        "prompt_expansion_enable": True,
        "audio_enable": False,
    }
    if images:
        kwargs["images"] = images[:10]

    try:
        response = await asyncio.to_thread(siray.video.generate_async, **kwargs)
        task_id = response.task_id
    except Exception as exc:
        busy.discard(chat_id)
        log.exception("submit failed")
        await update.message.reply_text(f"Submit failed: {exc}")
        return

    await update.message.reply_text(f"Queued.\nTask: {task_id}\nI'll send the clip when it's ready.")
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
                    await app.bot.send_message(chat_id, "Done, but Siray returned no file URL.")
                    return
                url = urls[0]
                try:
                    await app.bot.send_video(chat_id, video=url, caption="Done.")
                except Exception:
                    await app.bot.send_message(chat_id, f"Done:\n{url}")
                return
            if name == "FAILURE":
                reason = status.fail_reason or "unknown error"
                await app.bot.send_message(chat_id, f"Failed: {reason}")
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
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video))
    log.info("Bot polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
