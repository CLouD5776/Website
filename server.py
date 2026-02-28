#!/usr/bin/env python3
import http.server
import json
import os
import urllib.request
import urllib.parse
from datetime import datetime
from threading import Thread

PORT = 8080
ARCHIVE_FILE = 'archive.json'
FEEDS_FILE = 'feeds.json'
ARCHIVE_LIMIT = 500  # max items kept per feed

def load_json(filename, default):
    if os.path.exists(filename):
        try:
            with open(filename, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: could not load {filename}: {e}")
    return default

def save_json(filename, data):
    # Write to temp file first, then rename — prevents corruption on crash
    tmp = filename + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, filename)

def fetch_feed(feed_url):
    """Fetch a single feed via rss2json API"""
    api_url = f"https://api.rss2json.com/v1/api.json?rss_url={urllib.parse.quote(feed_url)}&api_key="
    try:
        req = urllib.request.Request(api_url, headers={'User-Agent': 'Supernova-Dashboard/2.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"Error fetching {feed_url}: {e}")
        return None

def update_archive():
    """Fetch all feeds and update the archive. New items are prepended; old items are NEVER deleted beyond the limit."""
    feeds = load_json(FEEDS_FILE, {})
    feeds_list = feeds.get('feeds', [])
    
    if not feeds_list:
        print("No feeds configured.")
        return
    
    # Load existing archive — this is our persistent store
    archive = load_json(ARCHIVE_FILE, {})
    
    total_new = 0
    for feed in feeds_list:
        feed_name = feed.get('name', 'Unknown')
        feed_url = feed.get('url', '')
        
        if not feed_url:
            continue
        
        result = fetch_feed(feed_url)
        if not result or result.get('status') != 'ok':
            print(f"Skipping {feed_name}: bad response")
            continue
        
        items = result.get('items', [])
        if not items:
            continue
        
        # Initialise this feed's archive bucket if it doesn't exist
        if feed_name not in archive:
            archive[feed_name] = []
        
        # Build a set of known links for fast deduplication
        existing_links = {item['link'] for item in archive[feed_name]}
        
        new_items = []
        for item in items:
            link = item.get('link', '')
            if not link or link in existing_links:
                continue
            
            # Stamp when we first saw this item
            item['archived_at'] = datetime.now().isoformat()
            item['feed_category'] = feed.get('category', 'News')
            item['feed_visibility'] = feed.get('visibility', 'both')
            
            new_items.append(item)
            existing_links.add(link)
        
        if new_items:
            # Prepend newest items; then trim to limit from the BACK (oldest dropped)
            archive[feed_name] = (new_items + archive[feed_name])[:ARCHIVE_LIMIT]
            total_new += len(new_items)
            print(f" + {len(new_items)} new from {feed_name}")
    
    save_json(ARCHIVE_FILE, archive)
    print(f"Archive updated at {datetime.now().isoformat()} — {total_new} new items total")

def background_archive_updater():
    """Run archive update every 5 minutes in background"""
    import time
    while True:
        try:
            update_archive()
        except Exception as e:
            print(f"Archive updater error: {e}")
        time.sleep(300)

class AdminHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()
    
    def log_message(self, fmt, *args):
        # Suppress noisy request logs; only print errors
        if args and str(args[1]) not in ('200', '304'):
            super().log_message(fmt, *args)
    
    def do_GET(self):
        if self.path == '/api/news':
            feeds = load_json(FEEDS_FILE, {})
            archive = load_json(ARCHIVE_FILE, {})
            feeds_list = feeds.get('feeds', [])
            
            # Build per-feed metadata map from current feeds.json
            # (visibility/category may have changed since archiving, so use live values)
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
                    # Use live metadata so changes in admin take effect immediately
                    item['feedName'] = feed_name
                    item['feedCategory'] = meta['category']
                    item['feedVisibility'] = meta['visibility']
                    all_items.append(item)
            
            # Sort newest first
            all_items.sort(key=lambda x: x.get('pubDate', ''), reverse=True)
            
            self._json_response(all_items)
        
        elif self.path == '/api/feeds':
            self._json_response(load_json(FEEDS_FILE, {}))
        
        elif self.path == '/api/archive':
            archive = load_json(ARCHIVE_FILE, {})
            # Return summary stats rather than full archive dump
            summary = {name: len(items) for name, items in archive.items()}
            total = sum(summary.values())
            self._json_response({'feeds': summary, 'total': total})
        
        elif self.path == '/api/update':
            Thread(target=update_archive).start()
            self._json_response({"status": "update triggered"})
        
        else:
            super().do_GET()
    
    def do_POST(self):
        if self.path == '/api/feeds':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            try:
                feeds = json.loads(post_data.decode('utf-8'))
                save_json(FEEDS_FILE, feeds)
                
                # Trigger archive update in background so the save feels instant
                Thread(target=update_archive).start()
                
                self._json_response({"status": "saved"})
            except Exception as e:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        else:
            self.send_error(404, "Not found")
    
    def _json_response(self, data):
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

if __name__ == '__main__':
    print(f"Starting Supernova on port {PORT}")
    
    # Initial archive fetch on startup
    Thread(target=update_archive).start()
    
    # Background updater
    Thread(target=background_archive_updater, daemon=True).start()
    
    server = http.server.ThreadingHTTPServer(("", PORT), AdminHTTPRequestHandler)
    print(f"Ready at http://localhost:{PORT}")
    server.serve_forever()
