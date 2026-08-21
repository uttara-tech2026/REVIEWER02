# Telegram Media Scheduler Bot

A simplified version: queue photos/videos, post them to one or more
destinations on a timer, no ads, no per-destination dedicated queues.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env: set BOT_TOKEN (from @BotFather)
python bot.py
```

State (queues, destinations, settings, stats) persists to `data.json` next
to the script, so restarting the bot doesn't wipe your queue — **except on
Railway and similar platforms, see below.**

## Deploying on Railway

1. Push this folder to a GitHub repo. **Do not commit `.env`** — it's in
   `.gitignore`. Set `BOT_TOKEN` as an environment variable in the
   Railway dashboard instead (Variables tab), plus any other settings
   from `.env.example` you want to override.
2. Railway will detect Python via `requirements.txt` and use the `Procfile`
   (`worker: python bot.py`) to start it as a background worker — it does
   **not** need a public port, since `run_polling()` isn't an HTTP server.
   If Railway shows a "no open port detected" warning, ignore it or set the
   service type to "Worker" explicitly in settings.
3. **Attach a Volume.** Railway's container filesystem resets on every
   redeploy. Without a volume, `data.json` (all your queues/settings/stats)
   and `media_cache/` (extracted zip files) disappear each time you push.
   In Railway: your service → Settings → Volumes → add one, mount it at
   e.g. `/data`, then set `DATA_FILE=/data/data.json` and
   `MEDIA_DIR=/data/media_cache` as environment variables.
4. Deploy. Check the Railway logs for `🤖 Bot running...` — if the token
   is wrong you'll see the error there immediately.

**Optional — `ffmpeg` for zip video duration.** Videos sent directly to the
bot already come with a duration from Telegram, so sorting/filtering/trimming
by length works out of the box for those. Videos bulk-added via `.zip` need
`ffprobe` (part of `ffmpeg`) to detect duration — the included
`nixpacks.toml` tells Railway's Nixpacks builder to install it. If it's
missing, the bot doesn't error; those zip-sourced videos just show up with
an unknown duration and get skipped by length filters / sorted last.

## How it fits together

- `config.py` — loads `.env`
- `storage.py` — JSON persistence, one record per chat
- `utils.py` — per-queue duplicate detection, zip extraction (+ ffprobe
  duration), queue sort/filter/trim helpers, delay math, caption resolution
- `scheduler.py` — APScheduler engine: each tick posts one item from the
  active queue to every enabled destination, reschedules with a fixed or
  random delay, stops + notifies when the active queue is empty
- `bot.py` — all Telegram command handlers + entrypoint, including the
  manual review flow (Send/Reject one-by-one via a second person)
- `nixpacks.toml` — optional, installs `ffmpeg` on Railway for zip-video
  duration detection

## Commands

**Queues**
- Send a photo/video → added to the active queue
- Send a `.zip` of photos/videos → bulk-extracted into the active queue
- `/queue` — active queue size + destinations
- `/clear` — empty active queue
- `/newqueue <name>` — create and switch to a new named queue
- `/queues` — list all queues, tap to switch
- `/sortqueue size|duration [asc|desc]` — sort the active queue. Items
  missing that field (e.g. photos have no duration) always sort last.
- `/filterqueue <min_sec> <max_sec>` — non-destructive; lists videos in the
  active queue whose duration falls in that range.
- `/trimqueue <min_sec> <max_sec>` — destructive; permanently removes
  videos **outside** that range from the active queue (asks for
  confirmation first via a button; photos and videos with unknown
  duration are always kept).

**Duplicate detection — per queue**
Duplicates are checked **within a single queue only**, using Telegram's
`file_unique_id`. The same photo/video can be added to two *different*
queues without being flagged — it's only rejected if you try to add it
twice to the *same* queue (even after it's already been sent and popped
out, so it can't be re-added to that queue by mistake).

**Destinations** (channels/groups you post to) — all broadcast mode: every
destination shares the same active queue and gets the same item each tick.
- `/adddest <name> <chat_id>` — register a destination
- `/destinations` — list, tap to enable/disable

If no destination is added, the bot posts back into the current chat.

**Sending**
- `/sendnow` — dump the whole active queue immediately, ignoring the interval
- `/startscheduler` — begin auto-posting at the configured interval
- `/stopscheduler` — stop auto-posting
- `/next` — countdown to the next scheduled post

**Dashboards**
- `/dashboard` — queue sizes, scheduler status, countdown, totals
- `/stats` — sent/failed counts per destination

**Captions**
- `/togglequeuecaption` — **ON: every post's caption is simply the name of
  the queue it came from.** While this is ON, the settings below (original
  caption, default caption, link/mention replace, last-token strip) are
  all skipped — queue name always wins.
- Every photo/video you send keeps its own caption automatically (when
  queue-name caption is OFF).
- `/togglecaption` — flip between: ON (default) = forward each item with the
  caption it originally had; OFF = always use the default caption instead.
- `/setcaption <text|clear>` — sets the fallback caption, used when
  original-caption mode is OFF, or when an item had no caption to begin with
  (e.g. media extracted from a zip never has one).
- `/togglelaststrip` — when ON, finds the **last space** in the outgoing
  caption, deletes everything after it, and replaces it with a fixed piece
  of text you set. Handy when people send items with their own promo tag
  or handle stuck on the end and you want to swap it for yours.
  - `/setlastwordreplacement <text|clear>` — the replacement text (`clear`
    just deletes the trailing token, replacing it with nothing).
- `/toggletglinkreplace` — when ON, finds **any** `t.me/...` link or
  `@mention` **anywhere** in the caption (not just at the end) and replaces
  it with a fixed piece of text you set.
  - `/settglinkreplacement <text|clear>` — the replacement text.
- `/previewcaption` — shows the before/after for the next item in the
  active queue, without sending anything, so you can check the rules do
  what you expect before turning the scheduler on.

**Manual review** (a second person approves/rejects each item before it's sent)
- `/setreviewer <chat_id>` — registers who reviews items. The reviewer
  must have started a chat with the bot at least once (Telegram bots
  can't message someone who's never opened a DM with them — same
  constraint the old `ADMIN_ID` had). They can get their own chat_id
  with `/id` in their own DM with the bot.
- `/startreview [queue]` — begins (or resumes) a review session on the
  given queue, defaulting to the active queue. Either the queue owner or
  the reviewer can run this — the reviewer runs it from their own DM,
  and the bot resolves it back to the owner's data automatically.
- `/stopreview` — pauses. Nothing is lost: whatever item is currently
  awaiting a decision just stays at the front of the queue untouched
  until you `/startreview` again.
- The reviewer is sent one item at a time with two buttons: **`<queue
  name>?`** to send, and **`NOT`** to reject.
  - **`<queue name>?`** → delivered to every enabled destination (or back
    into the owner's chat if none are set), with the caption **forced to
    the queue's name** — this always applies to review-approved sends,
    regardless of `/togglequeuecaption`.
  - **`NOT`** → the item is dropped from the queue and deleted (the
    file on disk too, if it came from a zip).
  - The bot immediately auto-advances to the next item. When the queue
    is empty, both sides get a "review complete" notice.
- Only one review session runs per chat at a time. Duplicate detection
  during review is the same per-queue rule as everywhere else — nothing
  extra to configure.

**Settings**
- `/setinterval fixed <seconds>`
- `/setinterval random <min> <max>`
- `/setmaxqueue <n>`
- `/setfiletypes photo,video`
- `/settimezone Asia/Kolkata` — any IANA timezone name
- `/setdatetimeformat <strftime format>`
- `/toggleshuffle` — random vs. in-order posting from the active queue
- `/settings` — show all current settings

**Notifications** (sent automatically)
- Queue completed / scheduler stopped
- Upload finished (`/sendnow`) and upload failed (per item)
- ZIP extraction completed
- Next-post progress after each auto-post

## What was removed from the original build (for simplicity)

- **Ads** — no ad rotation/insertion feature at all.
- **Dedicated per-destination queues** — all destinations now broadcast
  the same active queue; there's no `/setdestqueue` or per-destination
  shuffle anymore.
- **`/checkdest` health check** and admin DM notifications (`ADMIN_ID`).

## Known limitations / things to verify yourself

- **Not live-tested against Telegram's servers.** This was built and syntax/import
  checked in a sandbox with no network access to `api.telegram.org`, so please
  run it against your real bot token and watch the console for errors on first launch.
- `/trimqueue` only ever removes videos (by duration); photos and any item
  with an undetected duration are always kept, since there's nothing to
  measure them against.
- Duplicate detection is per-queue and based on Telegram's `file_unique_id`
  — it won't catch a pixel-identical image re-uploaded under a different
  file, and zip-extracted media isn't deduplicated at all (no stable id to
  key off of).
- Review sessions are one-at-a-time per chat — you can't have two people
  reviewing two different queues simultaneously under the current design
  (both would need to share the same `/setreviewer`-registered reviewer).
- If both `/togglelaststrip` and `/toggletglinkreplace` are ON at once,
  the link/mention replacement runs first, then the last-token
  strip/replace runs on whatever's left — so a link right at the end of a
  caption could get touched by both rules in sequence. Use
  `/previewcaption` to check the actual result before relying on it. (Both
  are skipped entirely if `/togglequeuecaption` is ON.)
