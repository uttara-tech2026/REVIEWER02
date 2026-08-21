"""
Simple JSON-file persistence layer.

Everything lives under one dict, keyed by chat_id (as string, since JSON
keys must be strings). Each chat gets:

{
  "queues": {"default": [ {unique_id, file_id, type, added_at,
                            size: int|None,       # bytes, when known
                            duration: float|None  # seconds, videos only
                           } ]},
  "active_queue": "default",

  # Duplicate tracking is PER QUEUE, not global: the same file can exist
  # in two different queues, but can't be added twice to the same queue.
  "queue_seen": {"default": ["<unique_id>", ...]},

  "destinations": {
      "name": {"chat_id": int, "enabled": bool, "sent": int, "failed": int}
  },
  "settings": {
      "mode": "fixed"|"random",
      "fixed_delay": int,
      "min_delay": int,
      "max_delay": int,
      "caption": str,
      "use_original_caption": bool,
      "use_queue_name_caption": bool,   # if True, every post's caption is
                                         # simply the active queue's name
                                         # (overrides everything else below)
      "max_queue_size": int,
      "allowed_types": [str],
      "timezone": str,
      "datetime_format": str,
      "shuffle": bool,   # random pop order for the queue
      "strip_last_word": bool,        # delete+replace everything after the
                                       # LAST space in the outgoing caption
      "last_word_replacement": str,   # text to put there instead (may be "")
      "replace_tg_links": bool,       # replace any t.me/@mention found
                                       # anywhere in the caption
      "tg_link_replacement": str,     # text to put there instead (may be "")
  },
  "scheduler_running": bool,
  "next_post_time": str|None,   # ISO timestamp, informational
  "stats": {"total_sent": int, "total_failed": int},

  # =========================
  # MANUAL REVIEW FLOW
  # =========================
  # reviewer_chat_id: the chat_id of the person who reviews items one by
  # one (must have DMed the bot at least once). Set via /setreviewer.
  # Destination(s) for approved items are the SAME shared "destinations"
  # dict above -- there's no separate destination for review.
  "reviewer_chat_id": int|None,
  "review_state": {
      "running": bool,
      "queue": str|None,   # which queue is currently under review
      "token": str|None,   # guards the currently-pending item's buttons
                           # against stale/double taps
  },
}

Top-level, alongside the per-chat entries, there's also:

  "_links": {"<reviewer_chat_id>": <owner_chat_id>}

This lets a reviewer run /startreview or /stopreview from THEIR OWN chat
with the bot and have it resolve back to the owner's queue/data, since
the reviewer's chat has no queues of its own.
"""

import json
import os
import asyncio
from datetime import datetime

import config

_lock = asyncio.Lock()
_data = None


def _default_chat():
    return {
        "queues": {"default": []},
        "active_queue": "default",
        "queue_seen": {"default": []},
        "destinations": {},
        "settings": {
            "mode": config.SCHEDULE_MODE,
            "fixed_delay": config.DEFAULT_SEND_DELAY,
            "min_delay": config.MIN_SEND_DELAY,
            "max_delay": config.MAX_SEND_DELAY,
            "caption": config.DEFAULT_CAPTION,
            "use_original_caption": True,
            "use_queue_name_caption": False,
            "max_queue_size": config.MAX_QUEUE_SIZE,
            "allowed_types": list(config.ALLOWED_FILE_TYPES),
            "timezone": config.SCHEDULE_TIMEZONE,
            "datetime_format": config.DATETIME_FORMAT,
            "shuffle": False,
            "strip_last_word": False,
            "last_word_replacement": "",
            "replace_tg_links": False,
            "tg_link_replacement": "",
        },
        "scheduler_running": False,
        "next_post_time": None,
        "stats": {"total_sent": 0, "total_failed": 0},
        "reviewer_chat_id": None,
        "review_state": {"running": False, "queue": None, "token": None},
    }


def _load():
    global _data
    if _data is not None:
        return _data
    if os.path.exists(config.DATA_FILE):
        try:
            with open(config.DATA_FILE, "r", encoding="utf-8") as f:
                _data = json.load(f)
        except (json.JSONDecodeError, OSError):
            _data = {}
    else:
        _data = {}
    return _data


def _save():
    tmp = config.DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_data, f, indent=2)
    os.replace(tmp, config.DATA_FILE)


async def get_chat(chat_id: int) -> dict:
    async with _lock:
        data = _load()
        key = str(chat_id)
        if key not in data:
            data[key] = _default_chat()
            _save()
        else:
            # migrate older saved chats that predate newer settings keys
            defaults = _default_chat()["settings"]
            for k, v in defaults.items():
                data[key].setdefault("settings", {}).setdefault(k, v)
            # migrate older destinations that predate this simplified schema
            for dest in data[key].get("destinations", {}).values():
                dest.pop("queue", None)
                dest.pop("shuffle", None)
                dest.pop("healthy", None)
                dest.pop("last_checked", None)
            data[key].setdefault("queue_seen", {})
            for qname in data[key].get("queues", {}):
                data[key]["queue_seen"].setdefault(qname, [])
            # migrate older chats that predate the review flow
            data[key].setdefault("reviewer_chat_id", None)
            data[key].setdefault("review_state", {"running": False, "queue": None, "token": None})
        return data[key]


async def save():
    async with _lock:
        _save()


async def all_chats() -> dict:
    async with _lock:
        return {k: v for k, v in _load().items() if not k.startswith("_")}


async def get_reviewer_link(reviewer_chat_id: int):
    """Given a reviewer's chat_id, returns the owner chat_id they review
    for, or None if this chat_id isn't registered as anyone's reviewer."""
    async with _lock:
        data = _load()
        return data.setdefault("_links", {}).get(str(reviewer_chat_id))


async def set_reviewer_link(reviewer_chat_id: int, owner_chat_id: int):
    async with _lock:
        data = _load()
        data.setdefault("_links", {})[str(reviewer_chat_id)] = owner_chat_id
        _save()


def now_str(chat: dict) -> str:
    """Format 'now' using the chat's configured timezone + format."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(chat["settings"]["timezone"])
    except Exception:
        tz = None
    dt = datetime.now(tz) if tz else datetime.now()
    try:
        return dt.strftime(chat["settings"]["datetime_format"])
    except Exception:
        return dt.isoformat()
