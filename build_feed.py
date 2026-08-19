#!/usr/bin/env python3
"""Build an RSS feed from a public LinkedIn profile.

ORIGIN: AI-generated (Claude Opus 5).

Reads the logged-out profile page, which embeds a JSON-LD graph of recent
posts (full text + timestamps). For posts not already cached, fetches the post
permalink once to pick up images and resolve the lnkd.in shortlinks. Everything
is kept in posts.json so each run only touches new posts.
"""

import html
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime

PROFILE = os.environ["PROFILE"]
AUTHOR = os.environ["AUTHOR"]
OUT_DIR = os.environ.get("OUT_DIR", "public")
CACHE = os.environ.get("CACHE_FILE", "posts.json")
KEEP = 60

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def get(url, tries=3):
    last = None
    for n in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (n + 1))
    raise RuntimeError("fetch failed %s: %s" % (url, last))


def profile_posts(page):
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)
    if not m:
        return []
    data = json.loads(html.unescape(m.group(1)))
    nodes = data.get("@graph", data if isinstance(data, list) else [data])
    out = []
    for n in nodes:
        if n.get("@type") != "DiscussionForumPosting":
            continue
        if (n.get("author") or {}).get("name") != AUTHOR:
            continue
        url = (n.get("url") or "").split("?")[0]
        if not url:
            continue
        out.append({
            "url": url,
            "text": n.get("text") or "",
            "date": n.get("datePublished") or "",
        })
    return out


def post_id(url):
    m = re.search(r"activity[:-](\d+)", url)
    return m.group(1) if m else url


def own_card(page):
    """A post page stacks several activity cards; keep only the first one.

    Each card carries a pair of `..._ellipsis-menu` markers ahead of its body,
    so the target post ends at the first marker that follows its own text.
    """
    marks = [m.start() for m in re.finditer("main-feed-activity-card__ellipsis-menu", page)]
    body = re.search(r"attributed-text-segment-list__content", page)
    start = body.start() if body else 0
    after = [m for m in marks if m > start]
    return page[:after[0]] if after else page


def resolve_short(short):
    """Follow a lnkd.in interstitial to the real destination."""
    try:
        page = get(short, tries=2)
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r'data-tracking-control-name="external_url_click"[^>]*?href="([^"]+)"', page)
    if not m:
        m = re.search(r'href="([^"]+)"[^>]*data-tracking-control-name="external_url_click"', page)
    return html.unescape(m.group(1)) if m else None


def enrich(url, text):
    """One fetch of the post page for images, plus one per shortlink in the text."""
    extra = {"images": [], "links": {}}
    try:
        page = get(url)
    except Exception as e:  # noqa: BLE001
        print("  ! enrich failed: %s" % e, file=sys.stderr)
        return extra

    card = own_card(page)
    imgs = []
    for u in re.findall(r'https://media[a-z.\-]*licdn\.com/dms/image/[^"\' ]+', card):
        u = html.unescape(u)
        if re.search(r"(feedshare|articleshare)", u) and u not in imgs:
            imgs.append(u)
    if not imgs:
        og = re.search(r'<meta property="og:image" content="([^"]+)"', page)
        if og:
            u = html.unescape(og.group(1))
            if re.search(r"licdn\.com/dms/image.*(feedshare|articleshare)", u):
                imgs.append(u)
    extra["images"] = imgs[:8]

    for short in sorted(set(re.findall(r'https://lnkd\.in/[A-Za-z0-9_\-]+', text or ""))):
        real = resolve_short(short)
        if real:
            extra["links"][short] = real
        time.sleep(1)
    return extra


def render_item(p):
    esc = html.escape
    body = []
    text = (p.get("text") or "").strip()
    for short, real in (p.get("links") or {}).items():
        text = text.replace(short, real)
    if text:
        safe = esc(text).replace("\n", "<br/>\n")
        safe = re.sub(r'(https?://[^\s<]+)', r'<a href="\1">\1</a>', safe)
        body.append("<p>%s</p>" % safe)
    for img in p.get("images", []):
        body.append('<p><img src="%s" alt=""/></p>' % esc(img))
    body.append('<p><a href="%s">View on LinkedIn</a></p>' % esc(p["url"]))
    content = "\n".join(body)

    title = " ".join(text.split())
    if len(title) > 90:
        title = title[:87].rsplit(" ", 1)[0] + "…"
    if not title:
        title = "LinkedIn post"

    try:
        dt = datetime.fromisoformat(p["date"].replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        dt = datetime.now(timezone.utc)

    return """    <item>
      <title>%s</title>
      <link>%s</link>
      <guid isPermaLink="true">%s</guid>
      <pubDate>%s</pubDate>
      <description><![CDATA[%s]]></description>
    </item>""" % (esc(title), esc(p["url"]), esc(p["url"]),
                  format_datetime(dt), content)


def main():
    cache = {}
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            cache = json.load(f)

    try:
        found = profile_posts(get(PROFILE))
    except Exception as e:  # noqa: BLE001
        if not cache:
            raise
        print("profile fetch failed (%s); rebuilding from cache" % e, file=sys.stderr)
        found = []
    print("profile: %d posts" % len(found))
    if not found and not cache:
        raise SystemExit("no posts found and no cache - refusing to write empty feed")

    new = 0
    for p in found:
        pid = post_id(p["url"])
        if pid in cache:
            if p["text"]:
                cache[pid]["text"] = p["text"]
            if p["date"]:
                cache[pid]["date"] = p["date"]
            continue
        print("  + new %s" % pid)
        p.update(enrich(p["url"], p["text"]))
        cache[pid] = p
        new += 1
        time.sleep(2)

    items = sorted(cache.values(), key=lambda x: x.get("date", ""), reverse=True)[:KEEP]
    cache = dict((post_id(p["url"]), p) for p in items)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, ensure_ascii=False)

    now = format_datetime(datetime.now(timezone.utc))
    body = "\n".join(render_item(p) for p in items)
    feed = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Notes</title>
    <link>%s</link>
    <atom:link href="%s" rel="self" type="application/rss+xml"/>
    <description>Saved posts, with full text and images.</description>
    <language>en</language>
    <lastBuildDate>%s</lastBuildDate>
    <ttl>60</ttl>
%s
  </channel>
</rss>
""" % (PROFILE, os.environ.get("FEED_URL", ""), now, body)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(feed)
    print("wrote %s/feed.xml - %d items, %d new" % (OUT_DIR, len(items), new))


if __name__ == "__main__":
    main()
