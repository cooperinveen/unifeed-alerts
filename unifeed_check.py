#!/usr/bin/env python3
"""
UN Unifeed watcher.

Checks the UN Unifeed homepage for newly published broadcast videos and posts
each new one to a Microsoft Teams channel via a Workflows ("incoming webhook")
Adaptive Card.

We read the server-rendered homepage (https://media.un.org/unifeed/en) rather
than the /search/ page or the official RSS feed:
  - /search/ is behind an anti-bot "Client Challenge" wall AND robots-disallowed.
  - the documented broadcaster RSS feed's URL is not publicly discoverable
    (every standard Drupal feed path 404s; likely JS-injected or login-gated).
The homepage is robots-allowed, server-rendered, and already carries the ~12
most recent assets (the same "last 12 videos" the RSS mirrors) with title,
synopsis, date, duration and thumbnail — everything a card needs, in one request.

State is kept in state.json (last-seen production date + recently-posted IDs) so
nothing is missed or double-posted even if a scheduled run is skipped.

Reads one secret from the environment:
  TEAMS_WEBHOOK_URL - Teams Workflows webhook URL (the UN Unifeed channel)

Zero third-party dependencies (stdlib only).
"""

import html
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

# --- config -----------------------------------------------------------------

HOME_URL = "https://media.un.org/unifeed/en"
ASSET_BASE = "https://media.un.org/unifeed/en/asset/"
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

# How many recently-posted IDs to remember (dedupe guard). ~11 videos/day, so
# 500 is roughly 45 days of history — the primary dedupe (see main()).
SEEN_IDS_CAP = 500

# On the very first run (no state file), seed silently instead of flooding the
# channel with the entire current homepage. Override by setting SEED_AND_POST=1.
SEED_AND_POST = os.environ.get("SEED_AND_POST") == "1"

# A browser-ish UA — the default urllib agent can get 403'd / challenged.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36 "
             "unifeed-alerts/1.0")


# --- helpers ----------------------------------------------------------------

def log(msg):
    print(msg, flush=True)


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        log(f"WARNING: could not read state file ({e}); treating as first run.")
        return None


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def clean_text(value):
    """Neutralise markdown so untrusted feed text can't render as links/markup
    in a Teams card. Teams TextBlocks render a subset of markdown, so a hostile
    string like '[click](http://evil)' would otherwise become a live link."""
    if not value:
        return ""
    out = str(value)
    for ch in "[]()`*_#>|":
        out = out.replace(ch, "\\" + ch)
    return out


def safe_https(value):
    """Return the URL only if it's a plain https:// link, else ''.
    Blocks javascript:, data:, http:, etc. from reaching a card button/image."""
    if not value:
        return ""
    v = str(value).strip()
    return v if v.lower().startswith("https://") else ""


def _strip_tags(raw):
    """Drop HTML tags, unescape entities, collapse whitespace to plain text."""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_dt(value):
    """Parse an ISO 8601 timestamp to an aware datetime, or None. Both the
    homepage <time datetime> and our saved watermark are ISO 8601, so a single
    path handles both."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _field(block, name):
    """Extract the first field__item text for a Drupal field--name-<name>."""
    m = re.search(r"field--name-" + re.escape(name) + r"\b.*?field__item[^>]*>(.*?)</div>",
                  block, re.S)
    return _strip_tags(m.group(1)) if m else ""


def fetch_og_image(asset_url):
    """Fetch an asset page and return its og:image (the canonical still), or ''.

    The homepage's NEWEST item is rendered in a 'hero' block
    (media-asset--top-video-unplayable) that omits the thumbnail — and that's
    exactly the item we post. The list items below it carry a thumb inline, but
    the featured one doesn't, so we pull og:image from the asset page instead.
    Only called for items actually being posted (≈never on an empty check), so
    the extra request is cheap. Best-effort: any failure just yields no image."""
    url = safe_https(asset_url)
    if not url:
        return ""
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "text/html", "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            page = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log(f"  (og:image fetch failed for {url}: {e})")
        return ""
    m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page)
    return m.group(1) if m else ""


def fetch_items():
    """Fetch and parse the homepage into a list of dicts (page order: newest
    first). Each <article> block is one asset card."""
    req = urllib.request.Request(
        HOME_URL, headers={"Accept": "text/html", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="replace")

    items = []
    for block in re.findall(r"<article\b.*?</article>", body, re.S):
        m_id = re.search(r"/unifeed/en/asset/([a-z0-9]+)/([a-z0-9]+)", block)
        if not m_id:
            continue
        prefix, asset_id = m_id.group(1), m_id.group(2)
        m_date = re.search(r'<time datetime="([^"]+)"', block)
        m_img = re.search(r'<img[^>]+src="(https://[^"]+\.jpg)"', block)
        items.append({
            "id": asset_id,
            "title": _field(block, "title"),
            "text": _field(block, "field-summary"),
            "duration": _field(block, "field-duration"),
            "link": f"{ASSET_BASE}{prefix}/{asset_id}",
            "thumb": m_img.group(1) if m_img else "",
            "pubDate": m_date.group(1) if m_date else "",
        })
    return items


def build_card(item):
    """Build the Teams Adaptive Card envelope for one UN Unifeed video."""
    title = clean_text(item.get("title"))
    text = clean_text(item.get("text"))
    link = safe_https(item.get("link"))
    thumb = safe_https(item.get("thumb"))

    when = parse_dt(item.get("pubDate"))
    when_str = when.strftime("%d %b %Y") if when else ""
    duration = item.get("duration") or ""
    subtitle = " · ".join(p for p in (when_str, duration) if p)

    body = [
        {"type": "TextBlock", "text": "🎬 New UN Unifeed video",
         "weight": "Bolder", "size": "Medium", "color": "Accent", "wrap": True},
    ]
    if title:
        body.append({"type": "TextBlock", "text": title, "weight": "Bolder",
                     "wrap": True, "spacing": "Small"})
    if subtitle:
        body.append({"type": "TextBlock", "text": subtitle, "isSubtle": True,
                     "spacing": "None", "size": "Small"})
    if thumb:
        body.append({"type": "Image", "url": thumb, "size": "Stretch",
                     "spacing": "Medium"})
    if text:
        body.append({"type": "TextBlock", "text": text, "wrap": True,
                     "spacing": "Medium"})

    actions = []
    if link:
        actions.append({"type": "Action.OpenUrl",
                        "title": "📄 View / Download on UN Unifeed", "url": link})

    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
        "actions": actions,
    }
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": card,
        }],
    }


def post_to_teams(webhook_url, card):
    payload = json.dumps(card).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


# --- main -------------------------------------------------------------------

def main():
    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL")
    if not webhook_url:
        log("ERROR: TEAMS_WEBHOOK_URL must be set.")
        return 1

    try:
        items = fetch_items()
    except Exception as e:
        log(f"ERROR fetching/parsing homepage: {e}")
        return 1

    log(f"Fetched {len(items)} item(s) from homepage.")
    if not items:
        # An empty parse usually means the markup changed or we got challenged;
        # don't advance state on a bad fetch.
        log("WARNING: parsed 0 items — treating as a bad fetch, not advancing state.")
        return 1

    state = load_state()
    first_run = state is None
    if first_run:
        state = {"last_published": None, "seen_ids": []}

    # Ordered list (oldest→newest) + set for O(1) lookup. Order matters so the
    # trim below keeps the genuinely most-recent IDs, not an arbitrary subset.
    seen_list = list(state.get("seen_ids") or [])
    seen_ids = set(seen_list)
    last_published = parse_dt(state.get("last_published"))

    # Homepage is newest-first; reverse so the channel reads chronologically.
    items = list(reversed(items))

    # NOTE: UNifeed production dates are DAY-GRANULAR (everything on a day is
    # T12:00:00Z), so the watermark uses strict `<` — never `<=`, which would
    # silently drop a new video published later the same day. seen_ids is the
    # authoritative dedupe; the watermark only prunes items already rolled off.
    new_items = []
    for item in items:
        item_id = item.get("id")
        if not item_id or item_id in seen_ids:
            continue
        pub = parse_dt(item.get("pubDate"))
        if last_published and pub and pub < last_published:
            continue
        new_items.append(item)

    # Decide whether to actually post.
    posting = not (first_run and not SEED_AND_POST)
    if first_run and not SEED_AND_POST:
        log(f"First run: seeding state with {len(new_items)} item(s), posting none. "
            f"(Set SEED_AND_POST=1 to post on a manual run.)")

    posted = 0
    for item in new_items:
        if posting:
            # The featured (newest) item's homepage block omits the thumbnail;
            # backfill from the asset page's og:image. Only for items we post.
            if not item.get("thumb"):
                item["thumb"] = fetch_og_image(item.get("link"))
            try:
                status = post_to_teams(webhook_url, build_card(item))
                log(f"Posted: {item.get('id')} @ {item.get('pubDate')} (HTTP {status})")
                posted += 1
            except Exception as e:
                log(f"ERROR posting {item.get('id')}: {e} — will retry next run.")
                # Don't record as seen, so the next run tries again.
                continue
        iid = item.get("id")
        if iid not in seen_ids:
            seen_ids.add(iid)
            seen_list.append(iid)
        pub = parse_dt(item.get("pubDate"))
        if pub and (last_published is None or pub > last_published):
            last_published = pub

    # Persist trimmed state.
    state["last_published"] = last_published.strftime("%Y-%m-%dT%H:%M:%SZ") if last_published else None
    # Keep the most recent IDs only (seen_list is oldest→newest).
    state["seen_ids"] = seen_list[-SEEN_IDS_CAP:]
    save_state(state)

    log(f"Done. New: {len(new_items)}, posted: {posted}, "
        f"watermark: {state['last_published']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
