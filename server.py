#!/usr/bin/env python3
"""
Supernova — RSS News Dashboard Server (v3.0)

feedparser-based backend. No third-party RSS APIs.
Runs on Pi, serves dashboard locally, archive bakes to static for Cloudflare.
"""

import os
import http.server
import json
import os.path
import re
import threading
from datetime import datetime
from time import mktime

import feedparser

PORT = 8080
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_FILE = os.path.join(_BASE_DIR, 'archive.json')
FEEDS_FILE = os.path.join(_BASE_DIR, 'feeds.json')
ARCHIVE_LIMIT = 500  # max items kept per feed (overridden by per-feed maxAge)

# Thread lock — prevents concurrent archive writes from losing data
_archive_lock = threading.Lock()


# ─── JSON HELPERS ─────────────────────────────────────────────────────────────

def load_json(filename, default):
    if os.path.exists(filename):
        try:
            with open(filename, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: could not load {filename}: {e}")
    return default

def save_json(filename, data):
    """Atomic write — temp file + rename prevents corruption on crash."""
    tmp = filename + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, filename)


# ─── DATE PARSING ─────────────────────────────────────────────────────────────

def parse_date(entry):
    """Extract a consistent ISO 8601 date from a feedparser entry.
    
    Fallback chain:
    1. published_parsed (time struct — most reliable)
    2. updated_parsed (some feeds use this instead)
    3. Parse raw published/updated string via email.utils
    4. datetime.now() (last resort — at least it's sortable)
    """
    for field in ('published_parsed', 'updated_parsed'):
        parsed = entry.get(field)
        if parsed:
            try:
                return datetime(*parsed[:6]).isoformat()
            except Exception:
                continue

    for field in ('published', 'updated'):
        raw = entry.get(field, '')
        if raw:
            try:
                from email.utils import parsedate_to_datetime
                return parsedate_to_datetime(raw).isoformat()
            except Exception:
                pass

    return datetime.now().isoformat()


# ─── MEDIA EXTRACTION ─────────────────────────────────────────────────────────

def extract_thumbnail(entry):
    """Extract best available image URL from a feedparser entry.
    
    Tries: media:thumbnail → media:content (image) → image enclosure → links
    """
    # media:thumbnail
    thumbs = entry.get('media_thumbnail', [])
    if thumbs and isinstance(thumbs, list) and len(thumbs) > 0:
        url = thumbs[0].get('url', '')
        if url:
            return url

    # media:content (often used for images)
    media = entry.get('media_content', [])
    if media and isinstance(media, list):
        for m in media:
            mtype = m.get('type', '')
            url = m.get('url', '')
            if url and ('image' in mtype or not mtype):
                return url

    # Image enclosure
    for enc in entry.get('enclosures', []):
        if 'image' in enc.get('type', ''):
            return enc.get('href', enc.get('url', ''))

    # Links with image type
    for link in entry.get('links', []):
        if 'image' in link.get('type', ''):
            return link.get('href', '')

    return None


def extract_enclosure(entry):
    """Extract audio/video enclosure if present (for podcasts etc.)."""
    for enc in entry.get('enclosures', []):
        etype = enc.get('type', '')
        href = enc.get('href', enc.get('url', ''))
        if href and 'image' not in etype:
            return {'link': href, 'type': etype}

    for link in entry.get('links', []):
        if link.get('rel') == 'enclosure':
            return {'link': link.get('href', ''), 'type': link.get('type', '')}

    return None


# ─── FEED FETCHING ────────────────────────────────────────────────────────────

def fetch_feed(feed_url):
    """Fetch a single feed via feedparser. Returns list of normalised items.
    
    Output shape matches what dashboard.html expects:
    {
        "title": str,
        "link": str,
        "pubDate": str (ISO 8601),
        "description": str,
        "thumbnail": str | None,
        "enclosure": {"link": ..., "type": ...} | None,
    }
    """
    try:
        feed = feedparser.parse(feed_url,
            request_headers={'User-Agent': 'Supernova-Dashboard/3.0'})

        if feed.bozo and not feed.entries:
            print(f"  Feed error for {feed_url}: {feed.bozo_exception}")
            return None

        items = []
        for entry in feed.entries:
            item = {
                'title': entry.get('title', '').strip(),
                'link': entry.get('link', '').strip(),
                'pubDate': parse_date(entry),
                'description': entry.get('summary', ''),
                'thumbnail': extract_thumbnail(entry),
                'enclosure': extract_enclosure(entry),
            }

            if not item['link']:
                continue

            items.append(item)

        return items

    except Exception as e:
        print(f"  Exception fetching {feed_url}: {e}")
        return None


# ─── KEYWORD FILTERING ────────────────────────────────────────────────────────

def matches_filters(item, feed_config):
    """Apply per-feed keyword include/exclude filters.
    
    feed_config may contain:
        "filters": {
            "include": ["keyword1", "keyword2"],   ← item must match at least one
            "exclude": ["keyword3"]                  ← item must match none
        }
    
    Matching is case-insensitive against title + description.
    If no filters defined, all items pass.
    """
    filters = feed_config.get('filters')
    if not filters:
        return True

    text = (item.get('title', '') + ' ' + item.get('description', '')).lower()

    # Exclude takes priority
    for kw in filters.get('exclude', []):
        if kw.lower() in text:
            return False

    # If include list exists, at least one must match
    includes = filters.get('include', [])
    if includes:
        return any(kw.lower() in text for kw in includes)

    return True


# ─── ARCHIVE UPDATE ───────────────────────────────────────────────────────────

# Track per-feed health for the /api/health endpoint
_feed_health = {}

def update_archive():
    """Fetch all feeds and update the archive.
    
    New items are prepended; oldest items are trimmed beyond the limit.
    Thread-safe via _archive_lock.
    """
    feeds = load_json(FEEDS_FILE, {})
    feeds_list = feeds.get('feeds', [])

    if not feeds_list:
        print("No feeds configured.")
        return

    with _archive_lock:
        archive = load_json(ARCHIVE_FILE, {})
        total_new = 0

        for feed in feeds_list:
            feed_name = feed.get('name', 'Unknown')
            feed_url = feed.get('url', '')

            if not feed_url:
                continue

            items = fetch_feed(feed_url)

            if items is None:
                _feed_health[feed_name] = {
                    'status': 'error',
                    'last_error': datetime.now().isoformat(),
                    'message': 'Feed returned no data',
                }
                print(f"  Skipping {feed_name}: bad response")
                continue

            # Apply keyword filters
            items = [i for i in items if matches_filters(i, feed)]

            if not items:
                _feed_health[feed_name] = {
                    'status': 'ok',
                    'last_success': datetime.now().isoformat(),
                    'items_fetched': 0,
                }
                continue

            if feed_name not in archive:
                archive[feed_name] = []

            existing_links = {item['link'] for item in archive[feed_name]}
            per_feed_limit = feed.get('maxItems', ARCHIVE_LIMIT)

            new_items = []
            for item in items:
                link = item.get('link', '')
                if not link or link in existing_links:
                    continue
                item['archived_at'] = datetime.now().isoformat()
                item['feed_category'] = feed.get('category', 'News')
                item['feed_visibility'] = feed.get('visibility', 'both')
                new_items.append(item)
                existing_links.add(link)

            if new_items:
                archive[feed_name] = (new_items + archive[feed_name])[:per_feed_limit]
                total_new += len(new_items)
                print(f"  + {len(new_items)} new from {feed_name}")

            _feed_health[feed_name] = {
                'status': 'ok',
                'last_success': datetime.now().isoformat(),
                'items_fetched': len(items),
                'new_items': len(new_items),
                'archived': len(archive.get(feed_name, [])),
            }

        save_json(ARCHIVE_FILE, archive)
        print(f"Archive updated at {datetime.now().isoformat()} — {total_new} new items total")


def background_archive_updater():
    """Run archive update every 5 minutes in background."""
    import time
    while True:
        try:
            update_archive()
        except Exception as e:
            print(f"Archive updater error: {e}")
        time.sleep(300)


# ─── HTTP SERVER ──────────────────────────────────────────────────────────────

class SupernovaHTTPHandler(http.server.SimpleHTTPRequestHandler):

    def end_headers(self):
        # CORS — allows Cloudflare-hosted static page to hit Pi API
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # No caching for API responses
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.end_headers()

    def log_message(self, fmt, *args):
        if args and str(args[1]) not in ('200', '204', '304'):
            super().log_message(fmt, *args)

    def do_GET(self):
        if self.path == '/api/news':
            self._serve_news()
        elif self.path == '/api/feeds':
            self._json_response(load_json(FEEDS_FILE, {}))
        elif self.path == '/api/archive':
            self._serve_archive_summary()
        elif self.path == '/api/health':
            self._serve_health()
        elif self.path == '/api/update':
            threading.Thread(target=update_archive).start()
            self._json_response({"status": "update triggered"})
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == '/api/feeds':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            try:
                feeds = json.loads(post_data.decode('utf-8'))
                # Strip blank entries before saving
                if 'feeds' in feeds:
                    feeds['feeds'] = [f for f in feeds['feeds'] if f.get('url', '').strip()]
                save_json(FEEDS_FILE, feeds)
                threading.Thread(target=update_archive).start()
                self._json_response({"status": "saved"})
            except Exception as e:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        else:
            self.send_error(404, "Not found")

    def _serve_news(self):
        feeds = load_json(FEEDS_FILE, {})
        archive = load_json(ARCHIVE_FILE, {})
        feeds_list = feeds.get('feeds', [])

        feed_meta = {}
        for feed in feeds_list:
            name = feed.get('name', '')
            feed_meta[name] = {
                'visibility': feed.get('visibility', 'both'),
                'category': feed.get('category', 'News'),
            }

        all_items = []
        for feed_name, items in archive.items():
            meta = feed_meta.get(feed_name, {'visibility': 'both', 'category': 'News'})
            for item in items:
                item['feedName'] = feed_name
                item['feedCategory'] = meta['category']
                item['feedVisibility'] = meta['visibility']
                all_items.append(item)

        all_items.sort(key=lambda x: x.get('pubDate', ''), reverse=True)
        self._json_response(all_items)

    def _serve_archive_summary(self):
        archive = load_json(ARCHIVE_FILE, {})
        summary = {name: len(items) for name, items in archive.items()}
        total = sum(summary.values())
        self._json_response({'feeds': summary, 'total': total})

    def _serve_health(self):
        """Per-feed health: last success/failure, item counts."""
        self._json_response({
            'server_time': datetime.now().isoformat(),
            'feeds': _feed_health,
        })

    def _json_response(self, data):
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print(f"Starting Supernova v3.0 on port {PORT}")
    threading.Thread(target=update_archive).start()
    threading.Thread(target=background_archive_updater, daemon=True).start()

    server = http.server.ThreadingHTTPServer(("", PORT), SupernovaHTTPHandler)
    os.chdir(_BASE_DIR)
    print(f"Ready at http://localhost:{PORT}")
    server.serve_forever()
