#!/usr/bin/env python3
"""Fetch news from RSS feeds and write data/latest.json for the morning brief site.

Stdlib-only so it can run in a clean GitHub Actions environment with no install step.
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from xml.etree import ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = REPO_ROOT / "data" / "latest.json"

FEEDS = [
    ("https://techcrunch.com/feed/",                              "🤖 TECH & AI",  "TechCrunch"),
    ("https://www.theverge.com/rss/index.xml",                    "🤖 TECH & AI",  "The Verge"),
    ("https://feeds.arstechnica.com/arstechnica/index",           "🤖 TECH & AI",  "Ars Technica"),
    ("https://hnrss.org/frontpage",                               "🤖 TECH & AI",  "Hacker News"),
    ("https://feeds.npr.org/1001/rss.xml",                        "🌎 TOP NEWS",   "NPR"),
    ("https://feeds.bbci.co.uk/news/rss.xml",                     "🌎 TOP NEWS",   "BBC News"),
    ("https://feeds.bbci.co.uk/news/world/rss.xml",               "🌍 WORLD",      "BBC News"),
    ("https://www.aljazeera.com/xml/rss/all.xml",                 "🌍 WORLD",      "Al Jazeera"),
    ("https://www.cnbc.com/id/100003114/device/rss/rss.html",     "📈 MARKETS",    "CNBC"),
    ("https://feeds.marketwatch.com/marketwatch/topstories/",     "📈 MARKETS",    "MarketWatch"),
    ("https://www.espn.com/espn/rss/news",                        "🏀 SPORTS",     "ESPN"),
    ("https://www.cbssports.com/rss/headlines/",                  "🏀 SPORTS",     "CBS Sports"),
]

PER_FEED_LIMIT = 4
PER_CATEGORY_LIMIT = 6
DESC_MAX_CHARS = 220
USER_AGENT = "morning-digest-bot/1.0 (+https://github.com/garciapn/morning-digest)"
TIMEOUT = 15


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def strip_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def parse_pubdate(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None


def time_ago(d: datetime | None, now: datetime) -> str:
    if d is None:
        return ""
    delta = now - d
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{int(hours)}h ago"
    days = delta.days
    if days < 7:
        return f"{days}d ago"
    return d.strftime("%b %-d") if sys.platform != "win32" else d.strftime("%b %#d")


def parse_feed(xml_bytes: bytes, category: str, source: str) -> list[dict]:
    out: list[dict] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return out

    # RSS 2.0
    items = root.findall(".//item")
    if items:
        for it in items[:PER_FEED_LIMIT]:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            desc = strip_html(it.findtext("description") or "")
            pub = parse_pubdate(it.findtext("pubDate"))
            if not title or not link:
                continue
            out.append({
                "title": title,
                "description": truncate(desc, DESC_MAX_CHARS),
                "source": source,
                "url": link,
                "category": category,
                "_pub": pub.isoformat() if pub else None,
            })
        return out

    # Atom
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = root.findall("a:entry", ns)
    for e in entries[:PER_FEED_LIMIT]:
        title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
        link_el = e.find("a:link", ns)
        link = link_el.get("href") if link_el is not None else ""
        summary = strip_html(e.findtext("a:summary", default="", namespaces=ns) or
                             e.findtext("a:content", default="", namespaces=ns))
        pub_str = e.findtext("a:updated", default="", namespaces=ns) or \
                  e.findtext("a:published", default="", namespaces=ns)
        pub = parse_pubdate(pub_str)
        if not title or not link:
            continue
        out.append({
            "title": title,
            "description": truncate(summary, DESC_MAX_CHARS),
            "source": source,
            "url": link,
            "category": category,
            "_pub": pub.isoformat() if pub else None,
        })
    return out


def main() -> int:
    now = datetime.now(timezone.utc)
    collected: list[dict] = []
    failures: list[str] = []

    for url, category, source in FEEDS:
        try:
            data = fetch(url)
            items = parse_feed(data, category, source)
            if not items:
                failures.append(f"{source} ({url}): no items parsed")
            collected.extend(items)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            failures.append(f"{source} ({url}): {e}")

    # Dedupe on URL
    seen_urls: set[str] = set()
    deduped: list[dict] = []
    for it in collected:
        if it["url"] in seen_urls:
            continue
        seen_urls.add(it["url"])
        deduped.append(it)

    # Sort each category by recency, cap per category
    by_cat: dict[str, list[dict]] = {}
    for it in deduped:
        by_cat.setdefault(it["category"], []).append(it)

    final: list[dict] = []
    for cat, items in by_cat.items():
        items.sort(key=lambda x: x["_pub"] or "", reverse=True)
        for it in items[:PER_CATEGORY_LIMIT]:
            pub_dt = parse_pubdate(it["_pub"]) if it["_pub"] else None
            it["timeAgo"] = time_ago(pub_dt, now)
            del it["_pub"]
            final.append(it)

    if not final:
        print("ERROR: no stories collected from any feed", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    # Read previous edition number to increment
    prev_edition = 0
    if DATA_FILE.exists():
        try:
            prev = json.loads(DATA_FILE.read_text())
            m = re.search(r"\d+", prev.get("edition", ""))
            if m:
                prev_edition = int(m.group(0))
        except (json.JSONDecodeError, OSError):
            pass

    payload = {
        "date": now.isoformat(),
        "edition": f"#{prev_edition + 1}",
        "totalStories": len(final),
        "podcast": {
            "show": "The Daily",
            "description": "The New York Times' daily news podcast — today's top story in 20 minutes."
        },
        "stories": final,
    }

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    print(f"Wrote {len(final)} stories to {DATA_FILE}")
    if failures:
        print(f"Note: {len(failures)} feed(s) had issues:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
