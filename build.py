#!/usr/bin/env python3
"""
Build script for The Daily Aggregate.
Fetches RSS feeds and Bluesky posts, generates a static index.html.
Run locally: python build.py
"""

import json
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from html import escape

import feedparser
import requests
from dateutil import parser as dateparser
from jinja2 import Template


CONFIG_PATH = Path(__file__).parent / "config.json"
TEMPLATE_PATH = Path(__file__).parent / "template.html"
OUTPUT_PATH = Path(__file__).parent / "index.html"

REQUEST_TIMEOUT = 15
USER_AGENT = "TheDailyAggregate/1.0 (Personal News Aggregator)"
BLUESKY_API = "https://public.api.bsky.app/xrpc"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_image(entry, summary_html):
    """Extract the best image URL from an RSS entry."""
    # 1. media:content or media:thumbnail
    media = getattr(entry, "media_content", None)
    if media:
        for m in media:
            url = m.get("url", "")
            if url and any(ext in url.lower() for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
                return url
            if url and m.get("medium") == "image":
                return url

    media_thumb = getattr(entry, "media_thumbnail", None)
    if media_thumb:
        for m in media_thumb:
            url = m.get("url", "")
            if url:
                return url

    # 2. enclosures
    enclosures = getattr(entry, "enclosures", [])
    for enc in enclosures:
        if enc.get("type", "").startswith("image/"):
            return enc.get("href") or enc.get("url", "")

    # 3. Parse <img> from summary or content HTML
    content_html = summary_html
    content_detail = getattr(entry, "content", None)
    if content_detail and isinstance(content_detail, list):
        content_html = content_detail[0].get("value", "") + content_html

    img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content_html)
    if img_match:
        url = img_match.group(1)
        if url.startswith("http"):
            return url

    # 4. og:image style links field
    links = getattr(entry, "links", [])
    for link in links:
        if link.get("type", "").startswith("image/"):
            return link.get("href", "")

    return ""


def fetch_feed(feed_info):
    """Fetch and parse a single RSS/Atom feed."""
    url = feed_info["url"]
    name = feed_info["name"]
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        items = []
        for entry in parsed.entries:
            pub_date = None
            for date_field in ("published_parsed", "updated_parsed"):
                t = getattr(entry, date_field, None)
                if t:
                    try:
                        pub_date = datetime(*t[:6], tzinfo=timezone.utc)
                    except Exception:
                        pass
                    break
            if pub_date is None:
                for date_str_field in ("published", "updated"):
                    ds = getattr(entry, date_str_field, None)
                    if ds:
                        try:
                            pub_date = dateparser.parse(ds)
                        except Exception:
                            pass
                        break

            summary_html = getattr(entry, "summary", "") or ""
            # Extract image from summary/content HTML before stripping tags
            image = extract_image(entry, summary_html)

            summary = re.sub(r"<[^>]+>", "", summary_html).strip()
            if len(summary) > 300:
                summary = summary[:297] + "..."

            items.append({
                "title": getattr(entry, "title", "Untitled"),
                "link": getattr(entry, "link", "#"),
                "summary": summary,
                "image": image,
                "source": name,
                "date": pub_date,
                "date_display": format_date(pub_date) if pub_date else "",
            })
        return items
    except Exception as e:
        print(f"  [WARN] Failed to fetch {name} ({url}): {e}")
        return []


def fetch_section_feeds(section_cfg):
    """Fetch all feeds for a section, merge and sort by date."""
    all_items = []
    feeds = section_cfg.get("feeds", [])
    for feed_info in feeds:
        print(f"  Fetching: {feed_info['name']}...")
        items = fetch_feed(feed_info)
        all_items.extend(items)
        print(f"    Got {len(items)} items")

    all_items.sort(key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    max_items = section_cfg.get("max_items", 15)
    return all_items[:max_items]


def fetch_section_with_keywords(section_cfg):
    """Fetch feeds, then boost items matching keywords."""
    all_items = []
    feeds = section_cfg.get("feeds", [])
    keywords = [k.lower() for k in section_cfg.get("keywords", [])]

    for feed_info in feeds:
        print(f"  Fetching: {feed_info['name']}...")
        items = fetch_feed(feed_info)
        all_items.extend(items)
        print(f"    Got {len(items)} items")

    # Score items by keyword relevance
    for item in all_items:
        text = f"{item['title']} {item['summary']}".lower()
        item["relevance"] = sum(1 for kw in keywords if kw in text)

    # Sort: relevant items first (by relevance desc, then date), then non-relevant by date
    relevant = [i for i in all_items if i["relevance"] > 0]
    general = [i for i in all_items if i["relevance"] == 0]

    relevant.sort(key=lambda x: (x["relevance"], x["date"] or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    general.sort(key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    max_items = section_cfg.get("max_items", 15)
    combined = relevant + general
    return combined[:max_items]


def fetch_bluesky_posts(section_cfg):
    """Fetch recent posts from Bluesky handles."""
    handles = section_cfg.get("bluesky_handles", [])
    max_per = section_cfg.get("max_posts_per_person", 3)
    all_posts = []

    for person in handles:
        handle = person["handle"]
        name = person["name"]
        print(f"  Fetching Bluesky: @{handle}...")
        try:
            url = f"{BLUESKY_API}/app.bsky.feed.getAuthorFeed"
            resp = requests.get(url, params={"actor": handle, "limit": max_per, "filter": "posts_no_replies"},
                                timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("feed", [])[:max_per]:
                post = item.get("post", {})
                record = post.get("record", {})
                text = record.get("text", "")
                created = record.get("createdAt", "")
                uri = post.get("uri", "")

                # Extract avatar
                author_info = post.get("author", {})
                avatar_url = author_info.get("avatar", "")

                # Extract embedded image
                post_image = ""
                embed = post.get("embed", {})
                embed_type = embed.get("$type", "")
                if embed_type == "app.bsky.embed.images#view":
                    images = embed.get("images", [])
                    if images:
                        post_image = images[0].get("thumb", "") or images[0].get("fullsize", "")
                elif embed_type == "app.bsky.embed.external#view":
                    ext = embed.get("external", {})
                    post_image = ext.get("thumb", "")

                # Build web URL from AT URI
                web_url = "#"
                if uri.startswith("at://"):
                    parts = uri.replace("at://", "").split("/")
                    if len(parts) >= 3:
                        did = parts[0]
                        rkey = parts[2]
                        web_url = f"https://bsky.app/profile/{handle}/post/{rkey}"

                pub_date = None
                if created:
                    try:
                        pub_date = dateparser.parse(created)
                    except Exception:
                        pass

                all_posts.append({
                    "author": name,
                    "handle": handle,
                    "text": text,
                    "link": web_url,
                    "image": post_image,
                    "avatar": avatar_url,
                    "date": pub_date,
                    "date_display": format_date(pub_date) if pub_date else "",
                    "likes": post.get("likeCount", 0),
                    "reposts": post.get("repostCount", 0),
                })
            print(f"    Got {min(max_per, len(data.get('feed', [])))} posts")
        except Exception as e:
            print(f"    [WARN] Failed for @{handle}: {e}")

    all_posts.sort(key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return all_posts


def format_date(dt):
    if dt is None:
        return ""
    now = datetime.now(timezone.utc)
    diff = now - dt
    if diff.total_seconds() < 3600:
        mins = int(diff.total_seconds() / 60)
        return f"{mins}m ago"
    elif diff.total_seconds() < 86400:
        hours = int(diff.total_seconds() / 3600)
        return f"{hours}h ago"
    elif diff.days < 7:
        return f"{diff.days}d ago"
    else:
        return dt.strftime("%b %d, %Y")


def build():
    config = load_config()
    sections = config["sections"]
    data = {}

    # Fetch AI News
    print("\n[AI News]")
    data["ai_news"] = fetch_section_feeds(sections["ai_news"])

    # Fetch World News
    print("\n[World News]")
    data["world_news"] = fetch_section_feeds(sections["world_news"])

    # Fetch Liverpool FC
    print("\n[Liverpool FC]")
    data["liverpool"] = fetch_section_feeds(sections["liverpool"])

    # Fetch AI Leaders Social
    print("\n[AI Leaders - Social]")
    data["ai_leaders_social"] = fetch_bluesky_posts(sections["ai_leaders_social"])

    # Fetch Yosemite Science
    print("\n[Yosemite & Sierra Nevada Science]")
    data["yosemite_science"] = fetch_section_with_keywords(sections["yosemite_science"])

    # Render template
    print("\n[Rendering HTML]")
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = Template(f.read())

    now = datetime.now(timezone.utc)
    html = template.render(
        site_title=config["site_title"],
        site_subtitle=config["site_subtitle"],
        last_updated=now.strftime("%B %d, %Y at %H:%M UTC"),
        sections=sections,
        data=data,
    )

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\nDone! Generated {OUTPUT_PATH}")
    total = sum(len(v) for v in data.values())
    print(f"Total items: {total}")


if __name__ == "__main__":
    build()
