#!/usr/bin/env python3
"""
Supernova — fetch_and_bake.py

Runs in GitHub Actions (or anywhere with feedparser installed).
Reads feeds.json, fetches all feeds, merges into archive.json,
then bakes a flat news.json for the Cloudflare static site.

No HTTP server. No Pi required. Designed to run every 30 minutes via cron.

Usage:
    pip install feedparser
    python3 fetch_and_bake.py
"""

import json
import os
import re
from datetime import datetime

import feedparser

FEEDS_FILE   = "feeds.json"
ARCHIVE_FILE = "archive.json"
NEWS_FILE    = "news.json"
ARCHIVE_LIMIT = 500


# ─── JSON HELPERS ─────────────────────────────────────────────────────────────

def load_json(filename, default):
    if os.path.exists(filename):
        try:
            with open(filename, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: could not load {filename}: {e}")
    return default

def save_json(filename, data, compact=False):
    tmp = filename + ".tmp"
    with open(tmp, "w") as f:
        if compact:
            json.dump(data, f, separators=(",", ":"))
        else:
            json.dump(data, f, indent=2)
    os.replace(tmp, filename)


# ─── DATE PARSING ─────────────────────────────────────────────────────────────

def parse_date(entry):
    for field in ("published_parsed", "updated_parsed"):
        parsed = entry.get(field)
        if parsed:
            try:
                return datetime(*parsed[:6]).isoformat()
            except Exception:
                continue

    for field in ("published", "updated"):
        raw = entry.get(field, "")
        if raw:
            try:
                from email.utils import parsedate_to_datetime
                return parsedate_to_datetime(raw).isoformat()
            except Exception:
                pass

    return datetime.now().isoformat()


# ─── MEDIA EXTRACTION ─────────────────────────────────────────────────────────

def extract_thumbnail(entry):
    thumbs = entry.get("media_thumbnail", [])
    if thumbs and isinstance(thumbs, list):
        url = thumbs[0].get("url", "")
        if url:
            return url

    media = entry.get("media_content", [])
    if media and isinstance(media, list):
        for m in media:
            mtype = m.get("type", "")
            url = m.get("url", "")
            if url and ("image" in mtype or not mtype):
                return url

    for enc in entry.get("enclosures", []):
        if "image" in enc.get("type", ""):
            return enc.get("href", enc.get("url", ""))

    for link in entry.get("links", []):
        if "image" in link.get("type", ""):
            return link.get("href", "")

    return None


def extract_enclosure(entry):
    for enc in entry.get("enclosures", []):
        etype = enc.get("type", "")
        href = enc.get("href", enc.get("url", ""))
        if href and "image" not in etype:
            return {"link": href, "type": etype}

    for link in entry.get("links", []):
        if link.get("rel") == "enclosure":
            return {"link": link.get("href", ""), "type": link.get("type", "")}

    return None


# ─── FEED FETCHING ────────────────────────────────────────────────────────────

def fetch_feed(feed_url):
    try:
        feed = feedparser.parse(
            feed_url,
            request_headers={"User-Agent": "Supernova-Dashboard/3.0"}
        )

        if feed.bozo and not feed.entries:
            print(f"  Feed error for {feed_url}: {feed.bozo_exception}")
            return None

        items = []
        for entry in feed.entries:
            item = {
                "title":       entry.get("title", "").strip(),
                "link":        entry.get("link", "").strip(),
                "pubDate":     parse_date(entry),
                "description": entry.get("summary", ""),
                "thumbnail":   extract_thumbnail(entry),
                "enclosure":   extract_enclosure(entry),
            }
            if not item["link"]:
                continue
            items.append(item)

        return items

    except Exception as e:
        print(f"  Exception fetching {feed_url}: {e}")
        return None


# ─── KEYWORD FILTERING ────────────────────────────────────────────────────────

def matches_filters(item, feed_config):
    filters = feed_config.get("filters")
    if not filters:
        return True

    text = (item.get("title", "") + " " + item.get("description", "")).lower()

    for kw in filters.get("exclude", []):
        if kw.lower() in text:
            return False

    includes = filters.get("include", [])
    if includes:
        return any(kw.lower() in text for kw in includes)

    return True


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print(f"fetch_and_bake starting at {datetime.now().isoformat()}")

    feeds_config = load_json(FEEDS_FILE, {})
    feeds_list = feeds_config.get("feeds", [])

    if not feeds_list:
        print("No feeds in feeds.json — nothing to do.")
        return

    archive = load_json(ARCHIVE_FILE, {})
    total_new = 0
    errors = 0

    for feed in feeds_list:
        feed_name = feed.get("name", "Unknown")
        feed_url  = feed.get("url", "").strip()

        if not feed_url:
            continue

        print(f"Fetching: {feed_name}")
        items = fetch_feed(feed_url)

        if items is None:
            print(f"  ✗ Failed: {feed_name}")
            errors += 1
            continue

        # Apply keyword filters
        items = [i for i in items if matches_filters(i, feed)]

        if feed_name not in archive:
            archive[feed_name] = []

        existing_links = {item["link"] for item in archive[feed_name]}
        per_feed_limit = feed.get("maxItems", ARCHIVE_LIMIT)

        new_items = []
        for item in items:
            link = item.get("link", "")
            if not link or link in existing_links:
                continue
            item["archived_at"]    = datetime.now().isoformat()
            item["feed_category"]  = feed.get("category", "News")
            item["feed_visibility"] = feed.get("visibility", "both")
            new_items.append(item)
            existing_links.add(link)

        if new_items:
            archive[feed_name] = (new_items + archive[feed_name])[:per_feed_limit]
            total_new += len(new_items)
            print(f"  + {len(new_items)} new items")
        else:
            print(f"  = No new items")

    # Save updated archive
    save_json(ARCHIVE_FILE, archive)
    print(f"\nArchive saved — {total_new} new items, {errors} feed errors")

    # Build feed metadata map for baking
    feed_meta = {}
    for feed in feeds_list:
        name = feed.get("name", "")
        feed_meta[name] = {
            "visibility": feed.get("visibility", "both"),
            "category":   feed.get("category", "News"),
        }

    # Flatten archive → news.json
    all_items = []
    for feed_name, items in archive.items():
        meta = feed_meta.get(feed_name, {"visibility": "both", "category": "News"})
        for item in items:
            item["feedName"]       = feed_name
            item["feedCategory"]   = meta["category"]
            item["feedVisibility"] = meta["visibility"]
            all_items.append(item)

    all_items.sort(key=lambda x: x.get("pubDate", ""), reverse=True)

    save_json(NEWS_FILE, all_items, compact=True)
    print(f"news.json written — {len(all_items)} total items ({os.path.getsize(NEWS_FILE) // 1024} KB)")


if __name__ == "__main__":
    main()
