"""
One AsyncIOScheduler shared across all chats. Each chat that has its
scheduler "running" gets a single one-shot job scheduled at a time;
after it fires we look at how much is left in the active queue and
either reschedule (fixed or random delay) or stop and notify.

All destinations broadcast: every scheduler tick pops ONE item from the
chat's active queue (FIFO, or randomly if settings.shuffle is on) and
sends the same copy to every enabled destination. If no destination is
configured, it posts back into the chat itself.
"""

from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import storage
import utils

scheduler = AsyncIOScheduler()


def start():
    if not scheduler.running:
        scheduler.start()


def _job_id(chat_id):
    return f"post_{chat_id}"


def _pop_item(chat, shuffle):
    """Pop one item from the active queue -- randomly if shuffle, else
    FIFO. Returns None if the queue is empty."""
    import random
    queue = chat["queues"].get(chat["active_queue"], [])
    if not queue:
        return None
    if shuffle:
        idx = random.randrange(len(queue))
        return queue.pop(idx)
    return queue.pop(0)


def _remaining(chat):
    return utils.queue_len(chat, chat["active_queue"])


async def _deliver(bot, chat_id, chat, name, dest, item, caption):
    """Send a single item to a single destination, updating stats and
    notifying in-chat on failure."""
    try:
        await utils.send_media_item(bot, dest["chat_id"], item, caption)
        dest["sent"] = dest.get("sent", 0) + 1
        chat["stats"]["total_sent"] += 1
        return True

    except Exception as e:
        dest["failed"] = dest.get("failed", 0) + 1
        chat["stats"]["total_failed"] += 1
        await bot.send_message(chat_id, f"❌ Upload failed to {name}: {e}")
        return False


async def _tick(bot, chat_id: int):
    """One posting tick for a chat: pop one item from the active queue
    and send it to every enabled destination. Returns (chat, sent_count)."""
    chat = await storage.get_chat(chat_id)
    settings = chat["settings"]

    enabled = {n: d for n, d in chat["destinations"].items() if d.get("enabled", True)}
    if not enabled:
        # nowhere configured -> fall back to posting into the chat itself
        enabled = {"this chat": {"chat_id": chat_id, "enabled": True, "sent": 0, "failed": 0}}
        chat["destinations"].setdefault("this chat", enabled["this chat"])

    sent_count = 0
    item = _pop_item(chat, settings.get("shuffle", False))
    if item:
        queue_name = chat["active_queue"]
        caption = utils.resolve_caption(item, queue_name, settings)
        for name, dest in enabled.items():
            if await _deliver(bot, chat_id, chat, name, dest, item, caption):
                sent_count += 1

    await storage.save()
    return chat, sent_count


async def _post_job(app, chat_id: int):
    bot = app.bot
    chat = await storage.get_chat(chat_id)

    if not chat.get("scheduler_running"):
        return

    if _remaining(chat) == 0:
        chat["scheduler_running"] = False
        chat["next_post_time"] = None
        await storage.save()
        await bot.send_message(chat_id, "✅ Queue completed — scheduler stopped.")
        return

    chat, sent_count = await _tick(bot, chat_id)

    remaining = _remaining(chat)
    if remaining > 0:
        delay = utils.next_delay(chat["settings"])
        run_at = datetime.now() + timedelta(seconds=delay)
        chat["next_post_time"] = run_at.isoformat()
        await storage.save()
        scheduler.add_job(
            _post_job, "date", run_date=run_at, args=[app, chat_id],
            id=_job_id(chat_id), replace_existing=True,
        )
        await bot.send_message(
            chat_id,
            f"📤 Sent {sent_count} item(s) this round. {remaining} left. "
            f"Next post in {utils.fmt_duration(delay)}.",
        )
    else:
        chat["scheduler_running"] = False
        chat["next_post_time"] = None
        await storage.save()
        await bot.send_message(chat_id, "✅ Queue completed — scheduler stopped.")


async def start_for_chat(app, chat_id: int):
    chat = await storage.get_chat(chat_id)
    if _remaining(chat) == 0:
        return False, "Queue is empty — nothing to schedule."

    chat["scheduler_running"] = True
    delay = utils.next_delay(chat["settings"])
    run_at = datetime.now() + timedelta(seconds=delay)
    chat["next_post_time"] = run_at.isoformat()
    await storage.save()

    scheduler.add_job(
        _post_job, "date", run_date=run_at, args=[app, chat_id],
        id=_job_id(chat_id), replace_existing=True,
    )
    return True, f"⏱ Scheduler started. First post in {utils.fmt_duration(delay)}."


async def stop_for_chat(chat_id: int):
    chat = await storage.get_chat(chat_id)
    chat["scheduler_running"] = False
    chat["next_post_time"] = None
    await storage.save()
    try:
        scheduler.remove_job(_job_id(chat_id))
    except Exception:
        pass
    return "🛑 Scheduler stopped."


async def send_now(bot, chat_id: int):
    """Drain the active queue immediately, ignoring the interval.
    Returns total items sent."""
    total_sent = 0
    while _remaining(await storage.get_chat(chat_id)) > 0:
        chat, sent_count = await _tick(bot, chat_id)
        if sent_count == 0:
            break
        total_sent += sent_count
    return total_sent
