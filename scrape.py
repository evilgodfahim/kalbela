#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import calendar
import email.utils
import types
from datetime import datetime, timezone, timedelta
import requests
import feedparser
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup

# ── Sources ────────────────────────────────────────────────────────────────

SOURCES = [
    "https://www.kalbela.com/rss/popular-rss.xml",
    "https://evilgodfahim.github.io/kal/articles.xml",
    "https://www.prothomalo.com/feed/"
]

# HTML pages scraped directly (not RSS)
HTML_SOURCES = [
    "https://www.kalbela.com/opinion",
]

FILES = {
    "opinion": "opinion.xml",
    "world": "world.xml",
    "daily": "daily.xml"
}

# ── FlareSolverr config ────────────────────────────────────────────────────

FLARE_URL = os.environ.get("FLARE_URL")
FLARE_API_KEY = os.environ.get("FLARE_API_KEY")
FLARE_SESSION = os.environ.get("FLARE_SESSION")
FLARE_MAX_TIMEOUT = int(os.environ.get("FLARE_MAX_TIMEOUT", "60000"))

_BD_TZ = timezone(timedelta(hours=6))  # Bangladesh Standard Time (UTC+6)

# ── HTML scraping: sections to strip before collecting links ───────────────
#
# These containers hold "popular" or "latest" news widgets that appear on the
# opinion category page but are NOT part of the main article listing.
#
_EXCLUDED_SELECTORS = [
    "#static_opinion",        # Featured/popular opinion carousel (flexslider)
    "#topnewsFlex",           # Top-news slider
    ".flex_latest",           # Latest-news ticker strip
    "section#breaking-news",  # Breaking-news banner
    "section#just-news",      # "Just in" news banner
]

# ── Utilities ──────────────────────────────────────────────────────────────

def load_existing(path):
    if not os.path.exists(path):
        root = ET.Element("rss", version="2.0")
        ET.SubElement(root, "channel")
        return root
    try:
        return ET.parse(path).getroot()
    except Exception:
        root = ET.Element("rss", version="2.0")
        ET.SubElement(root, "channel")
        return root


def format_pubdate(dt):
    if dt is None:
        dt = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return email.utils.format_datetime(dt.astimezone(_BD_TZ))


def parse_struct_time(st):
    ts = calendar.timegm(st)
    return datetime.fromtimestamp(ts, timezone.utc)


def ensure_utc(dt):
    if dt is None:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_entry_pubdt(entry):
    pp = getattr(entry, "published_parsed", None)
    if pp:
        try:
            return parse_struct_time(pp)
        except Exception:
            pass
    ps = getattr(entry, "published", None)
    if ps:
        try:
            dt = email.utils.parsedate_to_datetime(ps)
            return ensure_utc(dt)
        except Exception:
            pass
    return datetime.now(timezone.utc)


def get_item_pubdt(item):
    txt = item.findtext("pubDate")
    if not txt:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        dt = email.utils.parsedate_to_datetime(txt)
        return ensure_utc(dt)
    except Exception:
        try:
            dt = datetime.strptime(txt, "%a, %d %b %Y %H:%M:%S GMT")
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)


# ── FlareSolverr helpers ───────────────────────────────────────────────────

def fetch_via_flaresolverr(url, timeout_ms=FLARE_MAX_TIMEOUT):
    if not FLARE_URL:
        raise RuntimeError("FLARE_URL not configured")
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": int(timeout_ms),
        "userAgent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    if FLARE_SESSION:
        payload["session"] = FLARE_SESSION
    headers = {"Content-Type": "application/json"}
    if FLARE_API_KEY:
        headers["X-Api-Key"] = FLARE_API_KEY
    resp = requests.post(
        FLARE_URL, json=payload, headers=headers,
        timeout=(10, int(timeout_ms / 1000) + 10),
    )
    resp.raise_for_status()
    data = resp.json()
    sol = data.get("solution") or {}
    html = (
        sol.get("response")
        or sol.get("html")
        or data.get("response")
        or data.get("html")
    )
    if not html:
        raise RuntimeError(f"FlareSolverr returned no HTML for {url}: {data}")
    return html


def fetch_feed_text(url):
    if FLARE_URL:
        try:
            return fetch_via_flaresolverr(url)
        except Exception:
            pass
    r = requests.get(url, timeout=(5, 30))
    r.raise_for_status()
    return r.text


# ── HTML scraping ──────────────────────────────────────────────────────────

def scrape_html_opinion(url):
    """
    Fetch an opinion category HTML page and return article entries.

    Strips popular/latest-news containers (_EXCLUDED_SELECTORS) before
    collecting links so only the main article listing is harvested.

    Returns a list of SimpleNamespace objects with the same attributes that
    get_entry_pubdt() and merge_update_feed() expect from feedparser entries:
      .link, .id, .title, .published_parsed, .published
    """
    try:
        html = fetch_feed_text(url)
    except Exception:
        return []

    soup = BeautifulSoup(html, "html.parser")

    # Strip popular / latest sections before touching any links
    for sel in _EXCLUDED_SELECTORS:
        for el in soup.select(sel):
            el.decompose()

    # Prefer the main category listing div; fall back to <body>
    cat_page = soup.select_one("div.catagory-page") or soup.body
    if cat_page is None:
        return []

    entries = []
    seen = set()

    # Article structure on kalbela.com category pages:
    #
    #   <div class="news-content-box">
    #     <a aria-label="TITLE" href="/opinion/SUBCATEGORY/ID">
    #       <h5 class="titleShow ...">TITLE</h5>
    #     </a>
    #     <!-- trailing duplicate link with class="link" and no content -->
    #     <a aria-label="TITLE" class="link" href="..."></a>
    #   </div>
    #
    # Selecting h5.titleShow and walking up to the parent <a> gives us
    # only the real title links (not the empty trailing ones).

    for h5 in cat_page.select("div.news-content-box h5.titleShow"):
        anchor = h5.find_parent("a")
        if anchor is None:
            continue

        href = anchor.get("href", "").strip()
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.kalbela.com" + href

        if href in seen:
            continue
        seen.add(href)

        title = (anchor.get("aria-label") or "").strip() or h5.get_text(strip=True)

        # No pubDate is available on the listing page; get_entry_pubdt()
        # will fall through to datetime.now(timezone.utc).
        entries.append(types.SimpleNamespace(
            link=href,
            id=href,
            title=title,
            published_parsed=None,
            published=None,
        ))

    return entries


# ── Merge logic ────────────────────────────────────────────────────────────

def merge_update_feed(root, entries):
    channel = root.find("channel")
    if channel is None:
        channel = ET.SubElement(root, "channel")
    existing = {}
    for item in channel.findall("item"):
        link = item.findtext("link")
        if link:
            existing[link] = item
    for entry in entries:
        link = getattr(entry, "link", None) or getattr(entry, "id", None)
        if not link:
            continue
        link = link.strip()
        incoming_dt = get_entry_pubdt(entry)
        if link in existing:
            item = existing[link]
            if incoming_dt > get_item_pubdt(item):
                title_el = item.find("title")
                if title_el is None:
                    ET.SubElement(item, "title").text = getattr(entry, "title", "")
                else:
                    title_el.text = getattr(entry, "title", title_el.text)
                pd_el = item.find("pubDate")
                if pd_el is None:
                    ET.SubElement(item, "pubDate").text = format_pubdate(incoming_dt)
                else:
                    pd_el.text = getattr(entry, "published", format_pubdate(incoming_dt))
                guid_el = item.find("guid")
                if guid_el is None:
                    ET.SubElement(item, "guid", isPermaLink="false").text = link
                else:
                    guid_el.text = link
                channel.remove(item)
                channel.insert(0, item)
        else:
            item = ET.Element("item")
            ET.SubElement(item, "title").text = getattr(entry, "title", "")
            ET.SubElement(item, "link").text = link
            ET.SubElement(item, "pubDate").text = format_pubdate(get_entry_pubdt(entry))
            ET.SubElement(item, "guid", isPermaLink="false").text = link
            channel.insert(0, item)
            existing[link] = item
    all_items = channel.findall("item")
    for extra in all_items[500:]:
        channel.remove(extra)


# ── Main flow ──────────────────────────────────────────────────────────────

def collect_all_entries():
    all_entries = []

    # RSS / Atom sources
    for src in SOURCES:
        try:
            feed_text = None
            if FLARE_URL:
                feed_text = fetch_feed_text(src)
            if feed_text:
                feed = feedparser.parse(feed_text)
            else:
                feed = feedparser.parse(src)
            all_entries.extend(feed.entries)
        except Exception:
            continue

    # HTML sources scraped directly
    for src in HTML_SOURCES:
        all_entries.extend(scrape_html_opinion(src))

    return all_entries


def main():
    all_entries = collect_all_entries()

    # opinion
    op_root = load_existing(FILES["opinion"])
    op_entries = [
        e for e in all_entries
        if any(
            x in ((getattr(e, "link", None) or getattr(e, "id", None) or "").strip())
            for x in ["/opinion/", "/joto-mot-toto-path/"]
        )
    ]
    merge_update_feed(op_root, op_entries)
    ET.ElementTree(op_root).write(FILES["opinion"], encoding="utf-8", xml_declaration=True)

    # world
    wr_root = load_existing(FILES["world"])
    wr_entries = [
        e for e in all_entries
        if "/world/" in ((getattr(e, "link", None) or getattr(e, "id", None) or "").strip())
    ]
    merge_update_feed(wr_root, wr_entries)
    ET.ElementTree(wr_root).write(FILES["world"], encoding="utf-8", xml_declaration=True)

    # daily
    dl_root = load_existing(FILES["daily"])
    dl_entries = [
        e for e in all_entries
        if "/ajkerpatrika/" in ((getattr(e, "link", None) or getattr(e, "id", None) or "").strip())
    ]
    merge_update_feed(dl_root, dl_entries)
    ET.ElementTree(dl_root).write(FILES["daily"], encoding="utf-8", xml_declaration=True)


if __name__ == "__main__":
    main()
