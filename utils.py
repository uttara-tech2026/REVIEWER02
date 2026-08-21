import os
import zipfile
import random
import uuid
import shutil
import subprocess
import re

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm"}


def fmt_duration(seconds) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def next_delay(settings: dict) -> int:
    if settings["mode"] == "random":
        lo, hi = settings["min_delay"], settings["max_delay"]
        if lo > hi:
            lo, hi = hi, lo
        return random.randint(lo, hi)
    return settings["fixed_delay"]


def get_video_duration(path: str):
    """Best-effort video duration in seconds via ffprobe. Returns None if
    ffprobe isn't installed or the probe fails -- callers must tolerate
    None (e.g. size/duration-based sort puts these items last, filters
    just won't match them). Telegram-sent videos never need this since
    Telegram supplies duration directly on the Update object."""
    if not shutil.which("ffprobe"):
        return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=15,
        )
        return round(float(result.stdout.strip()), 1)
    except Exception:
        return None


# =========================
# PER-QUEUE DUPLICATE DETECTION
# =========================
# Duplicates are scoped to a single queue: the same file can exist in two
# different queues at once, but can't be added twice to the SAME queue.
def is_duplicate(chat: dict, queue_name: str, unique_id: str) -> bool:
    return unique_id in chat.setdefault("queue_seen", {}).get(queue_name, [])


def mark_seen(chat: dict, queue_name: str, unique_id: str):
    chat.setdefault("queue_seen", {}).setdefault(queue_name, [])
    chat["queue_seen"][queue_name].append(unique_id)
    # keep this bounded so the JSON file doesn't grow forever
    if len(chat["queue_seen"][queue_name]) > 3000:
        chat["queue_seen"][queue_name] = chat["queue_seen"][queue_name][-3000:]


def active_queue(chat: dict) -> list:
    return chat["queues"][chat["active_queue"]]


def queue_len(chat: dict, name: str = None) -> int:
    name = name or chat["active_queue"]
    return len(chat["queues"].get(name, []))


def sort_queue(queue: list, field: str, descending: bool = False):
    """In-place sort of a queue list by 'size' or 'duration'. Items missing
    the field (e.g. photos have no duration, or a zip video had no ffprobe
    available) always sort to the end, regardless of direction."""
    with_val = [i for i in queue if i.get(field) is not None]
    without_val = [i for i in queue if i.get(field) is None]
    with_val.sort(key=lambda i: i[field], reverse=descending)
    queue[:] = with_val + without_val


def filter_queue_by_duration(queue: list, min_s: float, max_s: float) -> list:
    """Non-destructive: returns matching items, queue is untouched.
    Only videos with a known duration can match."""
    if min_s > max_s:
        min_s, max_s = max_s, min_s
    return [
        item for item in queue
        if item.get("duration") is not None and min_s <= item["duration"] <= max_s
    ]


def trim_queue_by_duration(queue: list, min_s: float, max_s: float,
                            keep_unknown: bool = True) -> int:
    """Destructive: removes items outside [min_s, max_s] in-place.
    Photos / items with unknown duration are kept by default (nothing to
    measure them against). Returns the number of items removed."""
    if min_s > max_s:
        min_s, max_s = max_s, min_s
    kept, removed = [], 0
    for item in queue:
        d = item.get("duration")
        if d is None:
            if keep_unknown:
                kept.append(item)
            else:
                removed += 1
            continue
        if min_s <= d <= max_s:
            kept.append(item)
        else:
            removed += 1
    queue[:] = kept
    return removed


# =========================
# CAPTION PROCESSING
# =========================
TG_LINK_RE = re.compile(r"(https?://)?(t\.me|telegram\.me)/\S+", re.IGNORECASE)
TG_MENTION_RE = re.compile(r"(?<!\w)@\w{4,32}")


def strip_replace_last_token(caption: str, replacement: str) -> str:
    """Find the LAST space in the caption; delete everything from there
    to the end and replace it with `replacement`. If there's no space at
    all, the whole caption is treated as a single trailing token and gets
    replaced entirely. Pass an empty replacement to just delete it."""
    if not caption:
        return caption

    idx = caption.rfind(" ")
    prefix = caption[:idx] if idx != -1 else ""

    if replacement:
        return f"{prefix} {replacement}".strip() if prefix else replacement
    return prefix.strip()


def replace_tg_links(caption: str, replacement: str) -> str:
    """Replace any t.me/telegram.me link or @mention found ANYWHERE in the
    caption (not just at the end) with `replacement`. Empty replacement
    just removes them and tidies up leftover double spaces."""
    if not caption:
        return caption

    text = TG_LINK_RE.sub(replacement, caption)
    text = TG_MENTION_RE.sub(replacement, text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return text


def process_caption(caption: str, settings: dict) -> str:
    """Applies whichever caption-editing rules are enabled, in order:
    telegram-link/mention replacement first (can match anywhere), then
    last-token strip/replace (positional, meant for a trailing tag).
    Both are independently toggleable; a caption with neither enabled
    is returned unchanged."""
    if not caption:
        return caption

    if settings.get("replace_tg_links"):
        caption = replace_tg_links(caption, settings.get("tg_link_replacement", "") or "")
    if settings.get("strip_last_word"):
        caption = strip_replace_last_token(caption, settings.get("last_word_replacement", "") or "")
    return caption


def resolve_caption(item: dict, queue_name: str, settings: dict):
    """Works out the final caption for an item being posted from
    `queue_name`.

    - If use_queue_name_caption is ON: the caption is simply the queue's
      name, always -- nothing else runs (no original/default caption,
      no mangling rules).
    - Otherwise: original-caption-vs-default resolves first, then the
      mangling rules (link/mention replace, last-token strip) run on
      whatever that produced.
    """
    if settings.get("use_queue_name_caption"):
        return queue_name

    original_caption = item.get("caption")
    if settings.get("use_original_caption", True) and original_caption:
        caption = original_caption
    else:
        caption = settings.get("caption") or None
    return process_caption(caption, settings)


# =========================
# MEDIA DELIVERY
# =========================
async def send_media_item(bot, dest_chat_id, item: dict, caption):
    """Send a single queue item (photo/video) to dest_chat_id. Shared by
    the scheduler and the manual review flow so both send the same way.
    Raises on failure -- callers decide how to handle/report it."""
    if item.get("source") == "local":
        path = item["path"]
        with open(path, "rb") as f:
            if item["type"] == "video":
                await bot.send_video(dest_chat_id, f, caption=caption)
            else:
                await bot.send_photo(dest_chat_id, f, caption=caption)
    else:
        file_id = item["file_id"]
        if item["type"] == "video":
            await bot.send_video(dest_chat_id, file_id, caption=caption)
        else:
            await bot.send_photo(dest_chat_id, file_id, caption=caption)


async def send_media_item_with_buttons(bot, dest_chat_id, item: dict, caption, reply_markup):
    """Same as send_media_item, but attaches an inline keyboard -- used by
    the manual review flow to show Send/Reject buttons under the item."""
    if item.get("source") == "local":
        path = item["path"]
        with open(path, "rb") as f:
            if item["type"] == "video":
                await bot.send_video(dest_chat_id, f, caption=caption, reply_markup=reply_markup)
            else:
                await bot.send_photo(dest_chat_id, f, caption=caption, reply_markup=reply_markup)
    else:
        file_id = item["file_id"]
        if item["type"] == "video":
            await bot.send_video(dest_chat_id, file_id, caption=caption, reply_markup=reply_markup)
        else:
            await bot.send_photo(dest_chat_id, file_id, caption=caption, reply_markup=reply_markup)


# =========================
# ZIP EXTRACTION
# =========================
async def extract_zip_media(zip_path: str, extract_dir: str) -> list:
    """Extract image/video files from a zip. Returns list of
    {'type': 'photo'|'video', 'path': str} dicts. Skips anything that
    isn't a recognized image/video extension, and guards against
    zip-slip path traversal."""
    os.makedirs(extract_dir, exist_ok=True)
    results = []

    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            ext = os.path.splitext(name)[1].lower()
            if ext not in IMAGE_EXT and ext not in VIDEO_EXT:
                continue

            # zip-slip guard
            target = os.path.normpath(os.path.join(extract_dir, os.path.basename(name)))
            if not target.startswith(os.path.normpath(extract_dir)):
                continue

            # avoid collisions
            base, extn = os.path.splitext(target)
            if os.path.exists(target):
                target = f"{base}_{uuid.uuid4().hex[:6]}{extn}"

            with zf.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())

            media_type = "photo" if ext in IMAGE_EXT else "video"
            entry = {"type": media_type, "path": target}
            if media_type == "video":
                entry["duration"] = get_video_duration(target)
            results.append(entry)

    return results
