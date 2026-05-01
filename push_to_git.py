#!/usr/bin/env python3
"""
Supernova - Git Push Script
Triggered by OpenClaw agent via Discord.
Bakes archive.json into news.json, copies static files, and pushes to GitHub.
"""

import json
import os
import subprocess
from datetime import datetime

# ─── PATHS ───────────────────────────────────────────────────────────────────
# hello-world IS the git repo — no separate copy step needed
REPO_DIR     = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_FILE = os.path.join(REPO_DIR, "archive.json")
FEEDS_FILE   = os.path.join(REPO_DIR, "feeds.json")

# HTML files to commit — admin.html excluded (needs Pi API, useless on Cloudflare)
HTML_FILES = ["index.html", "dashboard.html", "feeds.json"]

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def run(cmd, cwd=REPO_DIR):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr.strip()}")
    return result.stdout.strip()

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def push_update():
    log("Starting push...")

    # 1. Load archive and feeds
    if not os.path.exists(ARCHIVE_FILE):
        raise FileNotFoundError(f"Archive not found: {ARCHIVE_FILE}")

    with open(ARCHIVE_FILE, "r") as f:
        archive = json.load(f)

    feeds = {}
    if os.path.exists(FEEDS_FILE):
        with open(FEEDS_FILE, "r") as f:
            feeds = json.load(f)

    # 2. Build feed metadata map (live visibility/category)
    feed_meta = {}
    for feed in feeds.get("feeds", []):
        name = feed.get("name", "")
        feed_meta[name] = {
            "visibility": feed.get("visibility", "both"),
            "category":   feed.get("category", "News"),
        }

    # 3. Flatten archive into a sorted list (same shape as /api/news)
    all_items = []
    for feed_name, items in archive.items():
        meta = feed_meta.get(feed_name, {"visibility": "both", "category": "News"})
        for item in items:
            item["feedName"]       = feed_name
            item["feedCategory"]   = meta["category"]
            item["feedVisibility"] = meta["visibility"]
            all_items.append(item)

    all_items.sort(key=lambda x: x.get("pubDate", ""), reverse=True)
    log(f"Archive contains {len(all_items)} items across {len(archive)} feeds.")

    # 4. Write news.json into repo
    news_path = os.path.join(REPO_DIR, "news.json")
    with open(news_path, "w") as f:
        json.dump(all_items, f, separators=(",", ":"))  # compact — smaller file
    log(f"Written news.json ({os.path.getsize(news_path) // 1024} KB)")

    # 5. Check if anything actually changed
    run(["git", "add", "news.json"] + HTML_FILES)
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=REPO_DIR
    )
    if diff.returncode == 0:
        log("No changes detected — nothing to push.")
        return "No changes — Cloudflare build skipped."

    # 6. Commit and push
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    commit_msg = f"Supernova update {timestamp} ({len(all_items)} articles)"
    run(["git", "commit", "-m", commit_msg])
    run(["git", "push", "origin", "main"])

    log(f"Pushed successfully: {commit_msg}")
    return f"✅ Pushed {len(all_items)} articles to GitHub. Cloudflare will deploy in ~30 seconds."


if __name__ == "__main__":
    try:
        result = push_update()
        print(result)
    except Exception as e:
        print(f"❌ Push failed: {e}")
