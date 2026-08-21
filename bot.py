import os
import time
import uuid
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import config
import storage
import scheduler
import utils

# =========================
# START / HELP
# =========================
HELP_TEXT = (
    "👋 Media Scheduler Bot\n\n"
    "QUEUE\n"
    "Send photos/videos to add them to the active queue.\n"
    "Send a .zip of photos/videos to bulk-add to the queue.\n"
    "/queue — show active queue\n"
    "/clear — clear active queue\n"
    "/newqueue <n> — create + switch to a new queue\n"
    "/queues — list queues, tap to switch\n"
    "/sortqueue size|duration [asc|desc] — sort active queue\n"
    "/filterqueue <min_sec> <max_sec> — list videos in a length range\n"
    "/trimqueue <min_sec> <max_sec> — permanently remove videos outside range\n\n"
    "DESTINATIONS\n"
    "/adddest <n> <chat_id> — add a channel/group\n"
    "/destinations — list, tap to enable/disable\n\n"
    "SENDING\n"
    "/sendnow — send the active queue immediately\n"
    "/startscheduler — begin auto-posting\n"
    "/stopscheduler — stop auto-posting\n"
    "/next — countdown to next scheduled post\n\n"
    "DASHBOARDS\n"
    "/dashboard — progress overview\n"
    "/stats — per-destination statistics\n\n"
    "CAPTIONS\n"
    "/togglequeuecaption — ON: every post's caption is simply the active\n"
    "  queue's name (overrides everything below while ON)\n"
    "/togglecaption — ON: keep each item's own caption when forwarding.\n"
    "  OFF: always use the default caption instead\n"
    "/setcaption <text|clear> — the default/fallback caption\n"
    "/togglelaststrip — delete+replace everything after the LAST space\n"
    "  in the outgoing caption (e.g. a trailing handle/link someone added)\n"
    "/setlastwordreplacement <text|clear> — what to put there instead\n"
    "/toggletglinkreplace — replace any t.me link/@mention found ANYWHERE\n"
    "  in the caption\n"
    "/settglinkreplacement <text|clear> — what to put there instead\n"
    "/previewcaption — see the before/after on the next queued item\n\n"
    "SETTINGS\n"
    "/setinterval fixed <seconds>\n"
    "/setinterval random <min> <max>\n"
    "/setmaxqueue <n>\n"
    "/setfiletypes photo,video\n"
    "/settimezone <tz e.g. Asia/Kolkata>\n"
    "/setdatetimeformat <strftime format>\n"
    "/toggleshuffle — random vs in-order posting from the active queue\n"
    "/settings — show current settings\n\n"
    "MANUAL REVIEW (one-by-one approve/reject by a second person)\n"
    "/setreviewer <chat_id> — who reviews items (they must have DMed\n"
    "  this bot at least once; run /id in their own chat to get it)\n"
    "/startreview [queue] — send the first item to the reviewer\n"
    "  (defaults to the active queue). Run by you or by the reviewer.\n"
    "/stopreview — pause; resume anytime with /startreview\n"
    "Reviewer taps <queue name>? (send, goes to your destinations, caption\n"
    "  = queue name) or NOT (reject, deleted). Bot auto-advances to next item.\n\n"
    "OTHER\n"
    "/id — show this chat's ID"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📌 Chat ID:\n{update.effective_chat.id}")


# =========================
# QUEUE MANAGEMENT
# =========================
async def new_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not context.args:
        await update.message.reply_text("❌ Usage: /newqueue <n>")
        return

    name = context.args[0]
    chat["queues"].setdefault(name, [])
    chat["queue_seen"].setdefault(name, [])
    chat["active_queue"] = name
    await storage.save()
    await update.message.reply_text(f"✅ Queue '{name}' created and set active.")


async def list_queues(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    buttons = []
    for name, items in chat["queues"].items():
        marker = "⭐ " if name == chat["active_queue"] else ""
        buttons.append([InlineKeyboardButton(
            f"{marker}{name} ({len(items)})", callback_data=f"useq:{name}"
        )])

    await update.message.reply_text(
        "📚 Queues (tap to switch):", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def switch_queue_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    chat = await storage.get_chat(query.message.chat_id)
    if name in chat["queues"]:
        chat["active_queue"] = name
        await storage.save()
        await query.edit_message_text(f"✅ Active queue is now '{name}'.")


async def queue_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)
    items = utils.active_queue(chat)

    dest_summary = ", ".join(
        f"{n}({'on' if d.get('enabled', True) else 'off'})"
        for n, d in chat["destinations"].items()
    ) or "none set — will send to this chat"

    shuffle_state = "🔀 shuffled" if chat["settings"].get("shuffle") else "➡️ in-order"
    await update.message.reply_text(
        f"📦 Queue '{chat['active_queue']}': {len(items)} items ({shuffle_state})\n"
        f"📤 Destinations: {dest_summary}"
    )


async def clear_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)
    chat["queues"][chat["active_queue"]] = []
    await storage.save()
    await update.message.reply_text("🗑 Cleared active queue!")


async def sort_queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not context.args:
        await update.message.reply_text(
            "❌ Usage: /sortqueue size|duration [asc|desc]\n"
            "Sorts the active queue. Items without that field (e.g. photos "
            "have no duration) are always placed last."
        )
        return

    field = context.args[0].lower()
    if field not in ("size", "duration"):
        await update.message.reply_text("❌ Sort field must be 'size' or 'duration'.")
        return

    order = context.args[1].lower() if len(context.args) > 1 else "asc"
    if order not in ("asc", "desc"):
        await update.message.reply_text("❌ Order must be 'asc' or 'desc'.")
        return

    queue = utils.active_queue(chat)
    utils.sort_queue(queue, field, descending=(order == "desc"))
    await storage.save()
    await update.message.reply_text(
        f"✅ Queue '{chat['active_queue']}' ({len(queue)} items) sorted by {field} ({order})."
    )


async def filter_queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage: /filterqueue <min_seconds> <max_seconds>\n"
            "Non-destructive — lists videos in the active queue whose "
            "duration falls in that range. Use /trimqueue to actually remove."
        )
        return

    try:
        lo, hi = float(context.args[0]), float(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ min/max must be numbers (seconds).")
        return

    queue = utils.active_queue(chat)
    matched = utils.filter_queue_by_duration(queue, lo, hi)

    if not matched:
        await update.message.reply_text(
            f"🔍 No videos between {lo:g}s and {hi:g}s in '{chat['active_queue']}'."
        )
        return

    lines = [f"🔍 {len(matched)} video(s) between {lo:g}s and {hi:g}s in '{chat['active_queue']}':"]
    for i, item in enumerate(matched[:20], 1):
        cap = (item.get("caption") or "")[:30]
        lines.append(f"{i}. {utils.fmt_duration(item['duration'])}" + (f" — {cap}" if cap else ""))
    if len(matched) > 20:
        lines.append(f"...and {len(matched) - 20} more.")
    await update.message.reply_text("\n".join(lines))


async def trim_queue_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage: /trimqueue <min_seconds> <max_seconds>\n"
            "Destructive — permanently removes videos OUTSIDE that duration "
            "range from the active queue (photos and unmeasured items are kept)."
        )
        return

    try:
        lo, hi = float(context.args[0]), float(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ min/max must be numbers (seconds).")
        return

    queue = utils.active_queue(chat)
    to_remove = sum(
        1 for i in queue
        if i.get("duration") is not None and not (min(lo, hi) <= i["duration"] <= max(lo, hi))
    )
    if to_remove == 0:
        await update.message.reply_text(
            f"✅ Nothing to remove — no videos fall outside {lo:g}-{hi:g}s."
        )
        return

    buttons = [[
        InlineKeyboardButton(f"✅ Remove {to_remove} item(s)", callback_data=f"trimq:{lo}:{hi}"),
        InlineKeyboardButton("❌ Cancel", callback_data="trimq:cancel"),
    ]]
    await update.message.reply_text(
        f"⚠️ This will permanently remove {to_remove} item(s) outside "
        f"{lo:g}-{hi:g}s from '{chat['active_queue']}'. Confirm?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def trim_queue_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")

    if parts[1] == "cancel":
        await query.edit_message_text("❌ Cancelled — nothing removed.")
        return

    lo, hi = float(parts[1]), float(parts[2])
    chat = await storage.get_chat(query.message.chat_id)
    queue = utils.active_queue(chat)
    removed = utils.trim_queue_by_duration(queue, lo, hi)
    await storage.save()
    await query.edit_message_text(
        f"🗑 Removed {removed} item(s) outside {lo:g}-{hi:g}s. "
        f"Queue '{chat['active_queue']}' now has {len(queue)} item(s)."
    )


# =========================
# MEDIA INGESTION
# =========================
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)
    settings = chat["settings"]
    active_queue_name = chat["active_queue"]

    media_type = None
    unique_id = None
    file_id = None
    size = None
    duration = None

    if update.message.photo:
        media_type = "photo"
        p = update.message.photo[-1]
        file_id, unique_id = p.file_id, p.file_unique_id
        size = p.file_size
    elif update.message.video:
        media_type = "video"
        v = update.message.video
        file_id, unique_id = v.file_id, v.file_unique_id
        size = v.file_size
        duration = v.duration

    if not file_id:
        return

    if media_type not in settings["allowed_types"]:
        await update.message.reply_text(f"❌ {media_type} not allowed by current settings.")
        return

    # Duplicate detection is per-queue: the same file can exist in other
    # queues, it just can't be added twice to THIS one.
    if utils.is_duplicate(chat, active_queue_name, unique_id):
        await update.message.reply_text(
            f"⚠️ Duplicate detected in '{active_queue_name}' — skipped."
        )
        return

    queue = chat["queues"][active_queue_name]
    if len(queue) >= settings["max_queue_size"]:
        await update.message.reply_text(
            f"❌ Queue full (max {settings['max_queue_size']}). Use /clear or /newqueue."
        )
        return

    queue.append({
        "type": media_type,
        "file_id": file_id,
        "unique_id": unique_id,
        "source": "telegram",
        "caption": update.message.caption,
        "added_at": storage.now_str(chat),
        "size": size,
        "duration": duration,
    })
    utils.mark_seen(chat, active_queue_name, unique_id)
    await storage.save()

    await update.message.reply_text(
        f"✅ Added to '{active_queue_name}'! Total: {len(queue)}"
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles .zip uploads: extract media inside and bulk-add to queue."""
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".zip"):
        return

    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)
    settings = chat["settings"]
    active_queue_name = chat["active_queue"]

    await update.message.reply_text("📥 Downloading zip...")
    os.makedirs(config.MEDIA_DIR, exist_ok=True)
    zip_path = os.path.join(config.MEDIA_DIR, f"{chat_id}_{int(time.time())}.zip")
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(zip_path)

    extract_dir = os.path.join(config.MEDIA_DIR, f"{chat_id}_{int(time.time())}")
    try:
        extracted = await utils.extract_zip_media(zip_path, extract_dir)
    except Exception as e:
        await update.message.reply_text(f"❌ ZIP extraction failed: {e}")
        return
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass

    queue = chat["queues"][active_queue_name]
    added, skipped = 0, 0
    for item in extracted:
        if item["type"] not in settings["allowed_types"]:
            skipped += 1
            continue
        if len(queue) >= settings["max_queue_size"]:
            skipped += 1
            continue
        try:
            size = os.path.getsize(item["path"])
        except OSError:
            size = None
        queue.append({
            "type": item["type"],
            "path": item["path"],
            "source": "local",
            "added_at": storage.now_str(chat),
            "size": size,
            "duration": item.get("duration"),  # None for photos, or if ffprobe unavailable
        })
        added += 1

    await storage.save()
    await update.message.reply_text(
        f"✅ ZIP extraction completed: {added} added, {skipped} skipped.\n"
        f"📦 Queue '{active_queue_name}': {len(queue)} items"
    )


# =========================
# DESTINATIONS
# =========================
async def add_destination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if len(context.args) < 2:
        await update.message.reply_text("❌ Usage: /adddest <name> <chat_id>")
        return

    name = context.args[0]
    try:
        dest_id = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid chat_id")
        return

    chat["destinations"][name] = {"chat_id": dest_id, "enabled": True, "sent": 0, "failed": 0}
    await storage.save()
    await update.message.reply_text(f"✅ Destination '{name}' -> {dest_id} added.")


async def list_destinations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not chat["destinations"]:
        await update.message.reply_text("No destinations set. Use /adddest <name> <chat_id>.")
        return

    buttons = []
    lines = ["📤 Destinations"]
    for name, d in chat["destinations"].items():
        state = "🟢 on" if d.get("enabled", True) else "🔴 off"
        lines.append(
            f"• {name} ({d['chat_id']}) — {state}  sent:{d['sent']} failed:{d['failed']}"
        )
        buttons.append([InlineKeyboardButton(
            f"Toggle {name}", callback_data=f"toggledest:{name}"
        )])

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def toggle_destination_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    chat = await storage.get_chat(query.message.chat_id)
    dest = chat["destinations"].get(name)
    if not dest:
        return
    dest["enabled"] = not dest.get("enabled", True)
    await storage.save()
    await query.edit_message_text(
        f"✅ '{name}' is now {'enabled 🟢' if dest['enabled'] else 'disabled 🔴'}."
    )


# =========================
# SENDING (manual immediate dump)
# =========================
async def send_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)
    total = utils.queue_len(chat, chat["active_queue"])

    if total == 0:
        await update.message.reply_text("⚠ Empty queue")
        return

    await update.message.reply_text(f"🚀 Sending {total} item(s) now (ignores interval)...")

    sent = await scheduler.send_now(context.bot, chat_id)
    await update.message.reply_text(f"✅ Upload finished: {sent} item-deliveries sent.")


async def start_scheduler_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ok, msg = await scheduler.start_for_chat(context.application, chat_id)
    await update.message.reply_text(msg)


async def stop_scheduler_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = await scheduler.stop_for_chat(chat_id)
    await update.message.reply_text(msg)


async def next_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not chat.get("scheduler_running") or not chat.get("next_post_time"):
        await update.message.reply_text("⏱ Scheduler is not running.")
        return

    run_at = datetime.fromisoformat(chat["next_post_time"])
    remaining = (run_at - datetime.now()).total_seconds()
    await update.message.reply_text(
        f"⏳ Next post in {utils.fmt_duration(remaining)} "
        f"(around {run_at.strftime(chat['settings']['datetime_format'])})."
    )


# =========================
# DASHBOARDS
# =========================
async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)
    s = chat["settings"]

    q_lines = [f"  • {n}: {len(items)} items" for n, items in chat["queues"].items()]

    if chat.get("scheduler_running") and chat.get("next_post_time"):
        run_at = datetime.fromisoformat(chat["next_post_time"])
        remaining = max(0, (run_at - datetime.now()).total_seconds())
        sched_line = f"🟢 running — next post in {utils.fmt_duration(remaining)}"
    else:
        sched_line = "🔴 stopped"

    mode_line = (
        f"fixed {s['fixed_delay']}s" if s["mode"] == "fixed"
        else f"random {s['min_delay']}-{s['max_delay']}s"
    )

    text = (
        "📊 Progress Dashboard\n\n"
        "Queues:\n" + "\n".join(q_lines) + "\n\n"
        f"Active queue: {chat['active_queue']}\n"
        f"Scheduler: {sched_line}\n"
        f"Interval mode: {mode_line}\n"
        f"Destinations: {len(chat['destinations'])} "
        f"({sum(1 for d in chat['destinations'].values() if d.get('enabled', True))} enabled)\n"
        f"Total sent: {chat['stats']['total_sent']} | "
        f"Total failed: {chat['stats']['total_failed']}"
    )
    await update.message.reply_text(text)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not chat["destinations"]:
        await update.message.reply_text("No destinations set yet.")
        return

    lines = ["📈 Destination statistics"]
    for name, d in chat["destinations"].items():
        lines.append(
            f"• {name} ({d['chat_id']}): sent {d['sent']}, failed {d['failed']}, "
            f"{'enabled' if d.get('enabled', True) else 'disabled'}"
        )
    await update.message.reply_text("\n".join(lines))


# =========================
# SETTINGS
# =========================
async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)
    args = context.args

    if not args:
        await update.message.reply_text(
            "❌ Usage:\n/setinterval fixed <seconds>\n/setinterval random <min> <max>"
        )
        return

    mode = args[0].lower()
    if mode == "fixed" and len(args) >= 2:
        try:
            chat["settings"]["mode"] = "fixed"
            chat["settings"]["fixed_delay"] = int(args[1])
        except ValueError:
            await update.message.reply_text("❌ Seconds must be a number.")
            return
        await storage.save()
        await update.message.reply_text(f"✅ Fixed interval set to {args[1]}s.")
    elif mode == "random" and len(args) >= 3:
        try:
            lo, hi = int(args[1]), int(args[2])
            chat["settings"]["mode"] = "random"
            chat["settings"]["min_delay"] = lo
            chat["settings"]["max_delay"] = hi
        except ValueError:
            await update.message.reply_text("❌ Min/max must be numbers.")
            return
        await storage.save()
        await update.message.reply_text(f"✅ Random interval set to {lo}-{hi}s.")
    else:
        await update.message.reply_text(
            "❌ Usage:\n/setinterval fixed <seconds>\n/setinterval random <min> <max>"
        )


async def toggle_queue_name_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    chat["settings"]["use_queue_name_caption"] = not chat["settings"].get("use_queue_name_caption", False)
    await storage.save()

    state = "ON" if chat["settings"]["use_queue_name_caption"] else "OFF"
    await update.message.reply_text(
        f"🏷 Queue-name caption is now {state}.\n"
        f"(When ON: every post's caption is simply its queue's name, and "
        f"the original/default caption and mangling rules are skipped "
        f"entirely while this is on.)"
    )


async def toggle_original_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    chat["settings"]["use_original_caption"] = not chat["settings"].get("use_original_caption", True)
    await storage.save()

    state = "ON" if chat["settings"]["use_original_caption"] else "OFF"
    await update.message.reply_text(
        f"✅ Forwarding with original caption is now {state}.\n"
        f"(When ON: each photo/video keeps the caption it was sent with. "
        f"When OFF, or if an item had no caption: the default caption from "
        f"/setcaption is used instead. Ignored entirely if /togglequeuecaption is ON.)"
    )


async def set_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not context.args:
        await update.message.reply_text("❌ Usage: /setcaption <text|clear>")
        return

    text = " ".join(context.args)
    chat["settings"]["caption"] = "" if text.lower() == "clear" else text
    await storage.save()
    await update.message.reply_text("✅ Default caption updated.")


async def toggle_last_word_strip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    chat["settings"]["strip_last_word"] = not chat["settings"].get("strip_last_word", False)
    await storage.save()

    state = "ON" if chat["settings"]["strip_last_word"] else "OFF"
    repl = chat["settings"].get("last_word_replacement") or "(nothing — just deletes it)"
    await update.message.reply_text(
        f"✂️ Last-token strip/replace is now {state}.\n"
        f"When ON: finds the LAST space in the outgoing caption, deletes "
        f"everything after it, and replaces it with: {repl}\n"
        f"Set the replacement text with /setlastwordreplacement <text|clear>."
    )


async def set_last_word_replacement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not context.args:
        await update.message.reply_text(
            "❌ Usage: /setlastwordreplacement <text|clear>\n"
            "This is the text used to replace whatever comes after the LAST "
            "space in a caption (e.g. a trailing @handle or link someone "
            "else put there). 'clear' means delete it with nothing."
        )
        return

    text = " ".join(context.args)
    chat["settings"]["last_word_replacement"] = "" if text.lower() == "clear" else text
    await storage.save()
    await update.message.reply_text(
        "✅ Last-token replacement set.\n"
        "(Reminder: this only applies once /togglelaststrip is ON, and is "
        "skipped entirely while /togglequeuecaption is ON.)"
    )


async def toggle_tg_link_replace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    chat["settings"]["replace_tg_links"] = not chat["settings"].get("replace_tg_links", False)
    await storage.save()

    state = "ON" if chat["settings"]["replace_tg_links"] else "OFF"
    repl = chat["settings"].get("tg_link_replacement") or "(nothing — just removes it)"
    await update.message.reply_text(
        f"🔗 Telegram link/mention replacement is now {state}.\n"
        f"When ON: any t.me link or @mention found ANYWHERE in the outgoing "
        f"caption gets replaced with: {repl}\n"
        f"Set the replacement text with /settglinkreplacement <text|clear>."
    )


async def set_tg_link_replacement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not context.args:
        await update.message.reply_text(
            "❌ Usage: /settglinkreplacement <text|clear>\n"
            "This text replaces every t.me link / @mention found anywhere "
            "in a caption. 'clear' means remove them with nothing."
        )
        return

    text = " ".join(context.args)
    chat["settings"]["tg_link_replacement"] = "" if text.lower() == "clear" else text
    await storage.save()
    await update.message.reply_text(
        "✅ Telegram link/mention replacement set.\n"
        "(Reminder: this only applies once /toggletglinkreplace is ON, and "
        "is skipped entirely while /togglequeuecaption is ON.)"
    )


async def preview_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows what the caption-processing rules would do to the next
    item's caption in the active queue, without sending anything."""
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)
    queue = utils.active_queue(chat)

    if not queue:
        await update.message.reply_text("⚠ Active queue is empty — nothing to preview.")
        return

    settings = chat["settings"]
    item = queue[0]

    if settings.get("use_queue_name_caption"):
        before = item.get("caption") or "(no caption)"
        after = chat["active_queue"]
    else:
        original_caption = item.get("caption")
        if settings.get("use_original_caption", True) and original_caption:
            before = original_caption
        else:
            before = settings.get("caption") or None
        after = utils.process_caption(before, settings)
        before = before or "(no caption)"
        after = after or "(no caption)"

    await update.message.reply_text(
        f"👀 Preview (next item in '{chat['active_queue']}'):\n\n"
        f"BEFORE:\n{before}\n\n"
        f"AFTER:\n{after}"
    )


async def set_max_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("❌ Usage: /setmaxqueue <n>")
        return

    chat["settings"]["max_queue_size"] = int(context.args[0])
    await storage.save()
    await update.message.reply_text(f"✅ Max queue size set to {context.args[0]}.")


async def set_file_types(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not context.args:
        await update.message.reply_text("❌ Usage: /setfiletypes photo,video")
        return

    types = [t.strip().lower() for t in " ".join(context.args).split(",") if t.strip()]
    valid = {"photo", "video"}
    if not types or not set(types).issubset(valid):
        await update.message.reply_text("❌ Allowed values: photo, video")
        return

    chat["settings"]["allowed_types"] = types
    await storage.save()
    await update.message.reply_text(f"✅ Allowed file types: {', '.join(types)}")


async def set_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not context.args:
        await update.message.reply_text("❌ Usage: /settimezone Asia/Kolkata")
        return

    tz = context.args[0]
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception:
        await update.message.reply_text("❌ Unknown timezone.")
        return

    chat["settings"]["timezone"] = tz
    await storage.save()
    await update.message.reply_text(f"✅ Timezone set to {tz}.")


async def set_datetime_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not context.args:
        await update.message.reply_text("❌ Usage: /setdatetimeformat %Y-%m-%d %H:%M:%S")
        return

    fmt = " ".join(context.args)
    try:
        datetime.now().strftime(fmt)
    except Exception:
        await update.message.reply_text("❌ Invalid strftime format.")
        return

    chat["settings"]["datetime_format"] = fmt
    await storage.save()
    await update.message.reply_text(f"✅ Datetime format set to: {fmt}")


async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)
    s = chat["settings"]

    text = (
        "⚙️ Settings\n"
        f"Mode: {s['mode']}\n"
        f"Fixed delay: {s['fixed_delay']}s\n"
        f"Random range: {s['min_delay']}-{s['max_delay']}s\n"
        f"Queue-name caption: {'ON' if s.get('use_queue_name_caption') else 'OFF'}\n"
        f"Default caption: {s['caption'] or '(none)'}\n"
        f"Forward with original caption: {'ON' if s.get('use_original_caption', True) else 'OFF'}\n"
        f"Max queue size: {s['max_queue_size']}\n"
        f"Allowed types: {', '.join(s['allowed_types'])}\n"
        f"Timezone: {s['timezone']}\n"
        f"Datetime format: {s['datetime_format']}\n"
        f"Shuffle active queue: {'ON' if s.get('shuffle') else 'OFF'}\n"
        f"Strip/replace last token: {'ON' if s.get('strip_last_word') else 'OFF'}"
        f" (replacement: {s.get('last_word_replacement') or '(none)'})\n"
        f"Replace tg links/@mentions: {'ON' if s.get('replace_tg_links') else 'OFF'}"
        f" (replacement: {s.get('tg_link_replacement') or '(none)'})"
    )
    await update.message.reply_text(text)


async def toggle_shuffle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    chat["settings"]["shuffle"] = not chat["settings"].get("shuffle", False)
    await storage.save()

    state = "ON" if chat["settings"]["shuffle"] else "OFF"
    await update.message.reply_text(
        f"🔀 Shuffle is now {state} for queue '{chat['active_queue']}'."
    )


# =========================
# MANUAL REVIEW FLOW
# =========================
async def _resolve_owner_chat(chat_id: int):
    """A command like /startreview can be run either by the queue owner
    (in their own chat, which has queues/settings) or by the reviewer (in
    THEIR own chat, which has none). Returns (owner_chat_id, chat_dict) or
    (None, None) if this chat_id is neither."""
    chat = await storage.get_chat(chat_id)
    if chat.get("reviewer_chat_id") is not None or chat["queues"] != {"default": []}:
        # Has been configured as an owner at some point (has a reviewer
        # set, or has ever queued something) -> treat as the owner chat.
        return chat_id, chat

    linked_owner = await storage.get_reviewer_link(chat_id)
    if linked_owner is not None:
        owner_chat = await storage.get_chat(linked_owner)
        return linked_owner, owner_chat

    return chat_id, chat  # fall back to treating this chat as the owner


async def set_reviewer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not context.args:
        await update.message.reply_text(
            "❌ Usage: /setreviewer <chat_id>\n"
            "The reviewer must have started a chat with this bot at least "
            "once (they can send /id in their own chat with the bot to get it)."
        )
        return

    try:
        reviewer_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid chat_id")
        return

    chat["reviewer_chat_id"] = reviewer_id
    await storage.set_reviewer_link(reviewer_id, chat_id)
    await storage.save()
    await update.message.reply_text(
        f"✅ Reviewer set to {reviewer_id}. Start a review with /startreview "
        f"[queue] (either of you can run it)."
    )


async def _push_next_review_item(bot, owner_chat_id: int, chat: dict):
    """Sends the next item in the under-review queue to the reviewer with
    Send/Reject buttons. If the queue is empty, stops the session and
    notifies both sides. Returns True if an item was sent."""
    state = chat["review_state"]
    queue_name = state["queue"]
    queue = chat["queues"].get(queue_name, [])
    reviewer_chat_id = chat.get("reviewer_chat_id")

    if not queue:
        state["running"] = False
        state["token"] = None
        await storage.save()
        await bot.send_message(owner_chat_id, f"✅ Review of '{queue_name}' complete — queue is empty.")
        if reviewer_chat_id:
            await bot.send_message(reviewer_chat_id, f"✅ Review of '{queue_name}' complete — no more items.")
        return False

    item = queue[0]
    token = uuid.uuid4().hex[:8]
    state["token"] = token
    await storage.save()

    caption = f"📥 Queue: {queue_name}\n" + (item.get("caption") or "(no caption)")
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{queue_name}?", callback_data=f"rev:{owner_chat_id}:send:{token}"),
        InlineKeyboardButton("NOT", callback_data=f"rev:{owner_chat_id}:reject:{token}"),
    ]])

    try:
        await utils.send_media_item_with_buttons(bot, reviewer_chat_id, item, caption, buttons)
    except Exception as e:
        await bot.send_message(owner_chat_id, f"❌ Failed to send item to reviewer: {e}")
        state["running"] = False
        state["token"] = None
        await storage.save()
        return False

    return True


async def start_review_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    owner_chat_id, chat = await _resolve_owner_chat(chat_id)

    reviewer_chat_id = chat.get("reviewer_chat_id")
    if not reviewer_chat_id:
        await update.message.reply_text(
            "❌ No reviewer set yet. Run /setreviewer <chat_id> first "
            "(from the queue owner's chat)."
        )
        return

    queue_name = context.args[0] if context.args else chat["active_queue"]
    if queue_name not in chat["queues"]:
        await update.message.reply_text(f"❌ No such queue '{queue_name}'.")
        return

    chat["review_state"] = {"running": True, "queue": queue_name, "token": None}
    await storage.save()

    sent = await _push_next_review_item(context.bot, owner_chat_id, chat)
    if sent:
        await update.message.reply_text(
            f"▶️ Review started for queue '{queue_name}'. First item sent to the reviewer."
        )
    else:
        await update.message.reply_text(f"⚠ Queue '{queue_name}' is empty — nothing to review.")


async def stop_review_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    owner_chat_id, chat = await _resolve_owner_chat(chat_id)

    chat["review_state"]["running"] = False
    await storage.save()
    await update.message.reply_text(
        "⏸ Review paused. The pending item (if any) stays put — resume with /startreview."
    )


async def review_decision_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")
    # rev:<owner_chat_id>:<send|reject>:<token>
    owner_chat_id = int(parts[1])
    action = parts[2]
    token = parts[3]

    chat = await storage.get_chat(owner_chat_id)
    state = chat.get("review_state") or {}

    if not state.get("running") or not token or state.get("token") != token:
        await query.answer("This item was already handled, or review isn't active.", show_alert=True)
        return

    await query.answer()

    # Invalidate immediately so a rapid double-tap on the same buttons
    # can't be processed twice.
    state["token"] = None
    await storage.save()

    queue_name = state["queue"]
    queue = chat["queues"].get(queue_name, [])
    if not queue:
        return
    item = queue.pop(0)

    if action == "send":
        caption = queue_name  # forced: review-approved sends always use the queue name
        enabled = {n: d for n, d in chat["destinations"].items() if d.get("enabled", True)}
        if not enabled:
            enabled = {"owner chat": {"chat_id": owner_chat_id, "enabled": True, "sent": 0, "failed": 0}}
            chat["destinations"].setdefault("owner chat", enabled["owner chat"])

        sent_ok = 0
        for name, dest in enabled.items():
            try:
                await utils.send_media_item(context.bot, dest["chat_id"], item, caption)
                dest["sent"] = dest.get("sent", 0) + 1
                chat["stats"]["total_sent"] += 1
                sent_ok += 1
            except Exception as e:
                dest["failed"] = dest.get("failed", 0) + 1
                chat["stats"]["total_failed"] += 1
                await context.bot.send_message(owner_chat_id, f"❌ Review-send failed to {name}: {e}")
        result_text = f"✅ Sent to {sent_ok}/{len(enabled)} destination(s)."
    else:
        if item.get("source") == "local":
            try:
                os.remove(item["path"])
            except OSError:
                pass
        result_text = "❌ Rejected — removed."

    try:
        await query.edit_message_caption(caption=result_text)
    except Exception:
        pass

    await storage.save()
    await _push_next_review_item(context.bot, owner_chat_id, chat)


# =========================
# MAIN
# =========================
def main():
    async def _on_startup(app):
        # AsyncIOScheduler needs a running event loop to attach to.
        # PTB doesn't create one until run_polling() starts, so we
        # start APScheduler here instead of in main().
        scheduler.start()

    app = ApplicationBuilder().token(config.BOT_TOKEN).post_init(_on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", get_chat_id))

    app.add_handler(CommandHandler("newqueue", new_queue))
    app.add_handler(CommandHandler("queues", list_queues))
    app.add_handler(CommandHandler("queue", queue_status))
    app.add_handler(CommandHandler("clear", clear_queue))
    app.add_handler(CommandHandler("sortqueue", sort_queue_cmd))
    app.add_handler(CommandHandler("filterqueue", filter_queue_cmd))
    app.add_handler(CommandHandler("trimqueue", trim_queue_prompt))

    app.add_handler(CommandHandler("adddest", add_destination))
    app.add_handler(CommandHandler("destinations", list_destinations))

    app.add_handler(CommandHandler("sendnow", send_now))
    app.add_handler(CommandHandler("startscheduler", start_scheduler_cmd))
    app.add_handler(CommandHandler("stopscheduler", stop_scheduler_cmd))
    app.add_handler(CommandHandler("next", next_post))

    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("stats", stats))

    app.add_handler(CommandHandler("setinterval", set_interval))
    app.add_handler(CommandHandler("togglequeuecaption", toggle_queue_name_caption))
    app.add_handler(CommandHandler("setcaption", set_caption))
    app.add_handler(CommandHandler("togglecaption", toggle_original_caption))
    app.add_handler(CommandHandler("togglelaststrip", toggle_last_word_strip))
    app.add_handler(CommandHandler("setlastwordreplacement", set_last_word_replacement))
    app.add_handler(CommandHandler("toggletglinkreplace", toggle_tg_link_replace))
    app.add_handler(CommandHandler("settglinkreplacement", set_tg_link_replacement))
    app.add_handler(CommandHandler("previewcaption", preview_caption))
    app.add_handler(CommandHandler("setmaxqueue", set_max_queue))
    app.add_handler(CommandHandler("setfiletypes", set_file_types))
    app.add_handler(CommandHandler("settimezone", set_timezone))
    app.add_handler(CommandHandler("setdatetimeformat", set_datetime_format))
    app.add_handler(CommandHandler("toggleshuffle", toggle_shuffle))
    app.add_handler(CommandHandler("settings", show_settings))

    app.add_handler(CommandHandler("setreviewer", set_reviewer))
    app.add_handler(CommandHandler("startreview", start_review_cmd))
    app.add_handler(CommandHandler("stopreview", stop_review_cmd))

    app.add_handler(CallbackQueryHandler(switch_queue_cb, pattern=r"^useq:"))
    app.add_handler(CallbackQueryHandler(toggle_destination_cb, pattern=r"^toggledest:"))
    app.add_handler(CallbackQueryHandler(trim_queue_cb, pattern=r"^trimq:"))
    app.add_handler(CallbackQueryHandler(review_decision_cb, pattern=r"^rev:"))

    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_media))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    print("🤖 Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
