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
DEFAULT_MODE = "r2v"
MODE_IDS = {
    "t2v": "alibaba/wan-3.0-t2v-spicy",
    "i2v": "alibaba/wan-3.0-i2v-spicy",
    "r2v": "alibaba/wan-3.0-ref2v-spicy",
}
DEFAULT_SIZE = os.environ.get("DEFAULT_SIZE", "480p")
DEFAULT_ASPECT = os.environ.get("DEFAULT_ASPECT", "16:9")
DEFAULT_DURATION = int(os.environ.get("DEFAULT_DURATION", "5"))

MAX_IMAGES = 10
MAX_VIDEOS = 5
MAX_AUDIOS = 5

if not TELEGRAM_BOT_TOKEN or not SIRAY_API_KEY:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN and SIRAY_API_KEY in the environment.")

siray = Siray(api_key=SIRAY_API_KEY)

DATA_PATH = Path(os.environ.get("BOT_DATA", "/tmp/wan-bot-data.json"))
refs: dict[int, dict[str, list[str]]] = {}
busy: set[int] = set()
pending: dict[int, dict] = {}
waiting: dict[int, dict] = {}
blocks: dict[str, dict[str, dict[str, str]]] = {}
last_jobs: dict[str, dict] = {}


def _load_store() -> None:
    if not DATA_PATH.exists():
        return
    try:
        raw = json.loads(DATA_PATH.read_text())
    except Exception:
        log.exception("could not read %s", DATA_PATH)
        return
    blocks.update(raw.get("blocks") or {})
    last_jobs.update(raw.get("last") or {})


def _save_store() -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps({"blocks": blocks, "last": last_jobs}))


_load_store()


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


def get_mode(context: ContextTypes.DEFAULT_TYPE) -> str:
    mode = str(context.chat_data.get("mode", DEFAULT_MODE)).lower()
    return mode if mode in MODE_IDS else DEFAULT_MODE


def get_model_id(context: ContextTypes.DEFAULT_TYPE) -> str:
    return MODE_IDS[get_mode(context)]


HELP_TEXT = (
    "Wan 3.0 spicy bot\n\n"
    "/model t2v|i2v|r2v — pick endpoint (default r2v)\n"
    "  t2v  prompt only, attachments ignored\n"
    "  i2v  needs 1 photo\n"
    "  r2v  needs ≥1 photo or video\n"
    "/g <prompt> — ask to run (y / n)\n"
    "/again — rerun last prompt; add size aspect duration to override\n"
    "/last — show last prompt\n"
    "/block save x — save pasted text, or `all` for the whole last prompt\n"
    "/blocks — list saved blocks\n"
    "/block x — print one block\n"
    "/settings 480p 16:9 5\n"
    "/size 480p|720p|1080p\n"
    "/duration 2-30\n"
    "/refs   /clear   /help   /howto   /balance   /task TASK_ID"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    uid = update.effective_user.id if update.effective_user else "?"
    mode = get_mode(context)
    await update.message.reply_text(
        f"Wan 3.0 spicy bot ready. Mode: {mode}\n"
        f"{MODE_IDS[mode]}\n\n"
        f"Your Telegram user id: {uid}\n\n"
        f"{HELP_TEXT}"
    )


async def howto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    await update.message.reply_text(
        "How to use this bot\n\n"
        "Modes\n"
        "/model t2v — prompt only. Photos are ignored.\n"
        "/model i2v — needs 1 photo, sent as the start frame.\n"
        "/model r2v — needs at least one photo or video.\n"
        "/model — show the current mode.\n\n"
        "Run a job\n"
        "/g your scene — does not send yet.\n"
        "Bot replies with mode, model, settings, ref counts, and prompt.\n"
        "y or /y sends. n or /n cancels.\n"
        "If mode is t2v and refs are attached, the confirm warns they will not be sent.\n\n"
        "Recover a prompt\n"
        "/last — print the last prompt.\n"
        "/again — restore its refs and ask to run again.\n"
        "/again 720p 9:16 8 — same prompt, those settings only.\n"
        "/again t2v 5 — same prompt, switch mode and duration.\n\n"
        "Blocks (x is a name you choose)\n"
        "/block save x — bot asks for text. Paste a slice, or send all for the whole last prompt. Then it asks for a short description.\n"
        "/block save x all — skip the paste and save the whole last prompt.\n"
        "/blocks — list names and descriptions.\n"
        "/block x — print that block.\n"
        "/block del x — delete it.\n\n"
        "Settings\n"
        "/settings 480p 16:9 5\n"
        "/size 720p\n"
        "/duration 8\n"
        "/refs — counts. /clear — drop refs.\n"
        "/balance — Siray USD. /task id — check a job.\n"
        "/help — short list. /howto — this guide."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    mode = get_mode(context)
    await update.message.reply_text(
        f"{HELP_TEXT}\n\nCurrent: {mode}\n{MODE_IDS[mode]}"
    )


async def show_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    args = [a.lower() for a in (context.args or [])]
    if args:
        raw = args[0].replace("ref2v", "r2v")
        if raw not in MODE_IDS:
            await update.message.reply_text("Use /model t2v, /model i2v, or /model r2v")
            return
        context.chat_data["mode"] = raw
        extra = ""
        if raw == "t2v":
            extra = "\nAttachments will be ignored on /generate."
        elif raw == "i2v":
            extra = "\nSend exactly 1 photo before /generate."
        else:
            extra = "\nSend at least one photo or video before /generate."
        await update.message.reply_text(
            f"Mode: {raw}\n{MODE_IDS[raw]}{extra}"
        )
        return
    mode = get_mode(context)
    await update.message.reply_text(f"Mode: {mode}\n{MODE_IDS[mode]}")


def fetch_balance() -> dict:
    client = getattr(siray, "_base_client", None)
    if client is not None and hasattr(client, "get"):
        return client.get("/v1/account/balance")
    import urllib.request

    req = urllib.request.Request(
        "https://api.siray.ai/v1/account/balance",
        headers={"Authorization": f"Bearer {SIRAY_API_KEY}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    try:
        payload = await asyncio.to_thread(fetch_balance)
    except Exception as exc:
        await update.message.reply_text(f"Balance lookup failed: {exc}")
        return
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        data = {}
    avail = data.get("available_balance", data.get("balance", "?"))
    cash = data.get("balance", "?")
    coupon = data.get("coupon_balance", "0")
    frozen = data.get("frozen_balance", "0")
    await update.message.reply_text(
        "Siray balance (USD)\n"
        f"available: ${avail}\n"
        f"wallet: ${cash}\n"
        f"coupons: ${coupon}\n"
        f"frozen: ${frozen}\n"
        "available = wallet + coupons − frozen"
    )


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


async def set_size(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    if not context.args:
        await update.message.reply_text(
            f"Size: {context.chat_data.get('size', DEFAULT_SIZE)}\nUsage: /size 480p"
        )
        return
    size = context.args[0]
    if size not in {"480p", "720p", "1080p"}:
        await update.message.reply_text("Size must be 480p, 720p, or 1080p")
        return
    context.chat_data["size"] = size
    await update.message.reply_text(f"Size: {size}")


async def set_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    if not context.args:
        await update.message.reply_text(
            f"Duration: {context.chat_data.get('duration', DEFAULT_DURATION)}s\n"
            "Usage: /duration 8"
        )
        return
    try:
        duration = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Duration must be an integer 2–30")
        return
    if duration < 2 or duration > 30:
        await update.message.reply_text("Duration must be 2–30")
        return
    context.chat_data["duration"] = duration
    await update.message.reply_text(f"Duration: {duration}s")


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
        await update.message.reply_text("Usage: /g your scene description")
        return
    await offer_job(update, context, prompt, None)


def parse_overrides(args: list[str]) -> dict:
    found: dict = {}
    for arg in args:
        low = arg.lower()
        if low in {"480p", "720p", "1080p"}:
            found["size"] = low
        elif low in {"16:9", "9:16", "1:1", "4:3", "3:4", "adaptive"}:
            found["aspect"] = low
        elif low.isdigit():
            found["duration"] = int(low)
        elif low in MODE_IDS:
            found["mode"] = low
    return found


async def again(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    chat_id = update.effective_chat.id
    last = last_jobs.get(str(chat_id))
    if not last:
        await update.message.reply_text("No last prompt yet. Run /g first.")
        return
    saved_refs = last.get("refs") or {}
    if any(saved_refs.get(k) for k in ("images", "videos", "audios")):
        refs[chat_id] = {
            "images": list(saved_refs.get("images") or []),
            "videos": list(saved_refs.get("videos") or []),
            "audios": list(saved_refs.get("audios") or []),
        }
    if last.get("mode") in MODE_IDS:
        context.chat_data["mode"] = last["mode"]
    await offer_job(update, context, last["prompt"], parse_overrides(context.args or []))


async def show_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    last = last_jobs.get(str(update.effective_chat.id))
    if not last:
        await update.message.reply_text("No last prompt yet.")
        return
    await update.message.reply_text(
        f"Last prompt ({last.get('mode')} {last.get('duration')}s "
        f"{last.get('size')} {last.get('aspect')}):\n\n{last.get('prompt')}"
    )


def confirm_text(job: dict) -> str:
    note = ""
    images = len(job["refs"]["images"])
    videos = len(job["refs"]["videos"])
    audios = len(job["refs"]["audios"])
    if job["mode"] == "t2v" and (images or videos or audios):
        note = (
            "\nMode is t2v, so those refs will NOT be sent. "
            "Reply n, then /model r2v and /again, to use them."
        )
    return (
        "About to run this prompt.\n"
        f"mode: {job['mode']}\n"
        f"model: {job['model_id']}\n"
        f"settings: {job['duration']}s {job['size']} {job['aspect']}\n"
        f"refs: images {images}, videos {videos}, audio {audios}\n"
        f"prompt: {job['prompt'][:500]}\n"
        f"{note}\n"
        "Reply y to send, n to cancel."
    )


async def offer_job(update, context, prompt: str, overrides: dict | None) -> None:
    chat_id = update.effective_chat.id
    if chat_id in busy:
        await update.message.reply_text("Already generating. Wait.")
        return
    overrides = overrides or {}
    mode = overrides.get("mode", get_mode(context))
    size = overrides.get("size", context.chat_data.get("size", DEFAULT_SIZE))
    aspect = overrides.get("aspect", context.chat_data.get("aspect", DEFAULT_ASPECT))
    duration = int(overrides.get("duration", context.chat_data.get("duration", DEFAULT_DURATION)))
    if duration < 2 or duration > 30:
        await update.message.reply_text("Duration must be 2–30")
        return
    b = bucket(chat_id)
    if mode == "i2v" and not b["images"]:
        await update.message.reply_text("i2v needs 1 photo first. Nothing was sent.")
        return
    if mode == "r2v" and not b["images"] and not b["videos"]:
        await update.message.reply_text("r2v needs at least one photo or video. Nothing was sent.")
        return
    job = {
        "prompt": prompt,
        "mode": mode,
        "model_id": MODE_IDS[mode],
        "size": size,
        "aspect": aspect,
        "duration": duration,
        "refs": {
            "images": list(b["images"]),
            "videos": list(b["videos"]),
            "audios": list(b["audios"]),
        },
    }
    pending[chat_id] = job
    await update.message.reply_text(confirm_text(job))


async def submit_pending(update, context) -> None:
    chat_id = update.effective_chat.id
    job = pending.pop(chat_id, None)
    if not job:
        await update.message.reply_text("Nothing waiting. Use /g or /again.")
        return
    if chat_id in busy:
        await update.message.reply_text("Already generating. Wait.")
        return
    context.chat_data["mode"] = job["mode"]
    context.chat_data["size"] = job["size"]
    context.chat_data["aspect"] = job["aspect"]
    context.chat_data["duration"] = job["duration"]
    last_jobs[str(chat_id)] = job
    _save_store()
    busy.add(chat_id)
    kwargs = {
        "model": job["model_id"],
        "prompt": job["prompt"],
        "duration": job["duration"],
        "size": job["size"],
        "aspect_ratio": job["aspect"],
        "prompt_expansion_enable": False,
        "audio_enable": True,
    }
    if job["mode"] == "i2v":
        kwargs["image"] = job["refs"]["images"][0]
        if len(job["refs"]["images"]) > 1:
            kwargs["end_image"] = job["refs"]["images"][1]
    elif job["mode"] == "r2v":
        if job["refs"]["images"]:
            kwargs["images"] = job["refs"]["images"][:MAX_IMAGES]
        if job["refs"]["videos"]:
            kwargs["videos"] = job["refs"]["videos"][:MAX_VIDEOS]
        if job["refs"]["audios"]:
            kwargs["audios"] = job["refs"]["audios"][:MAX_AUDIOS]
    await update.message.reply_text(
        f"Submitting {job['duration']}s {job['size']} {job['aspect']} · {job['mode']}"
    )
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


async def confirm_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    await submit_pending(update, context)


async def confirm_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    pending.pop(update.effective_chat.id, None)
    await update.message.reply_text("Cancelled. Last prompt is still available with /last.")


async def block_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    chat_key = str(update.effective_chat.id)
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage:\n/blocks\n/block save face\n/block save face all\n/block face\n/block del face"
        )
        return
    if args[0] == "save":
        if len(args) < 2:
            await update.message.reply_text("Usage: /block save face")
            return
        name = args[1].lower()
        if len(args) > 2 and args[2] == "all":
            last = last_jobs.get(chat_key)
            if not last:
                await update.message.reply_text("No last prompt to save.")
                return
            waiting[update.effective_chat.id] = {
                "kind": "desc",
                "name": name,
                "text": last["prompt"],
            }
            await update.message.reply_text("Short description for this block?")
            return
        waiting[update.effective_chat.id] = {"kind": "text", "name": name}
        await update.message.reply_text(
            "Send the text to save.\n"
            "Send all to save the whole last prompt.\n"
            "Send cancel to stop."
        )
        return
    if args[0] == "del" and len(args) > 1:
        blocks.get(chat_key, {}).pop(args[1].lower(), None)
        _save_store()
        await update.message.reply_text(f"Deleted {args[1].lower()}.")
        return
    item = blocks.get(chat_key, {}).get(args[0].lower())
    if not item:
        await update.message.reply_text("No block with that name. /blocks to list.")
        return
    await update.message.reply_text(f"{args[0].lower()} — {item.get('desc')}\n\n{item.get('text')}")


async def list_blocks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    owned = blocks.get(str(update.effective_chat.id), {})
    if not owned:
        await update.message.reply_text("No blocks saved.")
        return
    lines = [f"{name} — {item.get('desc') or 'no description'}" for name, item in owned.items()]
    await update.message.reply_text("\n".join(lines))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update) or not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    low = text.lower()
    wait = waiting.get(chat_id)
    if wait:
        if low == "cancel":
            waiting.pop(chat_id, None)
            await update.message.reply_text("Block save cancelled.")
            return
        if wait["kind"] == "text":
            if low == "all":
                last = last_jobs.get(str(chat_id))
                if not last:
                    await update.message.reply_text("No last prompt. Paste the text instead.")
                    return
                body = last["prompt"]
            else:
                body = text
            waiting[chat_id] = {"kind": "desc", "name": wait["name"], "text": body}
            await update.message.reply_text("Short description for this block?")
            return
        if wait["kind"] == "desc":
            chat_key = str(chat_id)
            blocks.setdefault(chat_key, {})[wait["name"]] = {
                "text": wait["text"],
                "desc": text[:80],
            }
            waiting.pop(chat_id, None)
            _save_store()
            await update.message.reply_text(f"Saved block {wait['name']}.")
            return
    if chat_id in pending and low in {"y", "yes", "n", "no"}:
        if low in {"y", "yes"}:
            await submit_pending(update, context)
        else:
            pending.pop(chat_id, None)
            await update.message.reply_text("Cancelled. Last prompt is still available with /last.")


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
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("howto", howto))
    app.add_handler(CommandHandler("balance", show_balance))
    app.add_handler(CommandHandler("model", show_model))
    app.add_handler(CommandHandler("refs", show_refs))
    app.add_handler(CommandHandler("clear", clear_refs))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("size", set_size))
    app.add_handler(CommandHandler("duration", set_duration))
    app.add_handler(CommandHandler("g", generate))
    app.add_handler(CommandHandler("generate", generate))
    app.add_handler(CommandHandler("again", again))
    app.add_handler(CommandHandler("last", show_last))
    app.add_handler(CommandHandler("block", block_cmd))
    app.add_handler(CommandHandler("blocks", list_blocks))
    app.add_handler(CommandHandler("y", confirm_yes))
    app.add_handler(CommandHandler("n", confirm_no))
    app.add_handler(CommandHandler("task", lookup_task))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(
        MessageHandler(filters.VIDEO | filters.ANIMATION | filters.Document.VIDEO, on_video)
    )
    app.add_handler(
        MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.AUDIO, on_audio)
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
