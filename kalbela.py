#!/usr/bin/env python3
"""
scrape_kalbela.py
-----------------
Scrapes kalbela.com/opinion for all opinion articles and generates
an Inoreader-compatible RSS 2.0 feed with RFC 2822 pub dates.

Date strategy
    The listing page does not render per-article timestamps.
    Every article image filename encodes a UTC Unix timestamp:
        /assets/news_photos/2026/08/20/image_320468_1787230722.webp
                                                           ^^^^^^^^^^
    That number is parsed and used as <pubDate>.  Falls back to the
    YYYY/MM/DD in the path (midnight UTC), then to current time.

State
    Scraped articles are persisted in feed_state.json so they stay
    in the feed for RETENTION_DAYS even after dropping off the live
    page.  New scrapes win on conflicting keys.
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Config ───────────────────────────────────────────────────────────────────

BASE_URL       = "https://www.kalbela.com"
OPINION_URL    = f"{BASE_URL}/opinion"
OUTPUT_XML     = "kalbela_opinion.xml"
STATE_FILE     = "feed_state.json"
RETENTION_DAYS = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "bn-BD,bn;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cache-Control": "no-cache",
}

# ── Date helpers ──────────────────────────────────────────────────────────────

_RE_IMG_TS    = re.compile(r"/image[_-]\d+[_-](\d{9,11})\.")
_RE_IMG_PATH  = re.compile(r"/news_photos/(\d{4})/(\d{2})/(\d{2})/")
_LAZY_MARKER  = "lazy-logo.png"


def _pub_date_from_image(img_src: str) -> str:
    """
    Try three sources in order:
    1. Unix timestamp embedded in the image filename.
    2. YYYY/MM/DD directory path (midnight UTC).
    3. Current UTC time as fallback.
    """
    m = _RE_IMG_TS.search(img_src)
    if m:
        return formatdate(int(m.group(1)), usegmt=True)

    m = _RE_IMG_PATH.search(img_src)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ts = datetime(y, mo, d, tzinfo=timezone.utc).timestamp()
        return formatdate(ts, usegmt=True)

    return formatdate(usegmt=True)


# ── Scraper ───────────────────────────────────────────────────────────────────

_OPINION_RE = re.compile(
    r"^https://www\.kalbela\.com/opinion/[A-Za-z0-9_-]+/\d+$"
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def scrape(html: str) -> list[dict]:
    """
    Returns a list of article dicts from the opinion listing page.
    Only /opinion/{subcategory}/{id} URLs are included;
    /ajkerpatrika/ sister-publication articles are excluded.
    """
    soup = BeautifulSoup(html, "html.parser")
    articles: list[dict] = []
    seen: set[str] = set()

    for h5 in soup.find_all("h5", class_="titleShow"):
        parent_a = h5.parent
        # The <h5> must be a direct child of <a href="OPINION_URL">
        if parent_a is None or parent_a.name != "a":
            continue

        url = (parent_a.get("href") or "").strip()
        if not _OPINION_RE.match(url):
            continue
        if url in seen:
            continue
        seen.add(url)

        # Walk up to the card container (first ancestor that has an img.news_img)
        card = h5
        for _ in range(12):
            card = card.parent
            if card is None:
                break
            if card.find("img", class_="news_img"):
                break

        if card is None:
            continue

        # ── Image ──────────────────────────────────────────────────────────
        img_tag = card.find("img", class_="news_img")
        raw_src = img_url = ""
        if img_tag:
            raw_src = (
                img_tag.get("src")
                or img_tag.get("data-original")
                or ""
            )
            if _LAZY_MARKER in raw_src:
                # JS replaced real src with placeholder; fall back to data-original
                raw_src = img_tag.get("data-original") or ""
            if raw_src:
                img_url = (
                    raw_src
                    if raw_src.startswith("http")
                    else BASE_URL + raw_src
                )

        # ── Summary ────────────────────────────────────────────────────────
        summary_div = card.find("div", class_="summery")
        summary = (
            _clean(summary_div.get_text(" ", strip=True))[:800]
            if summary_div
            else ""
        )

        # ── Subcategory from URL: /opinion/<sub>/<id> ──────────────────────
        subcategory = url.rstrip("/").split("/")[-2]

        articles.append(
            {
                "url":        url,
                "title":      _clean(h5.get_text(" ", strip=True)),
                "summary":    summary,
                "image_url":  img_url,
                "pub_date":   _pub_date_from_image(raw_src),
                "subcategory": subcategory,
            }
        )

    return articles


# ── State management ──────────────────────────────────────────────────────────

def _load_state() -> dict[str, dict]:
    p = Path(STATE_FILE)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[WARN] could not load state: {exc}", file=sys.stderr)
        return {}


def _save_state(state: dict[str, dict]) -> None:
    Path(STATE_FILE).write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _too_old(rfc2822: str) -> bool:
    try:
        dt = parsedate_to_datetime(rfc2822)
        return dt < datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    except Exception:
        return False


def _sort_key(art: dict) -> datetime:
    try:
        return parsedate_to_datetime(art["pub_date"])
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


# ── RSS builder ───────────────────────────────────────────────────────────────

def _cdata(text: str) -> str:
    """Wrap text in CDATA, safely escaping any embedded ]]>."""
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _build_rss(articles: list[dict]) -> str:
    items: list[str] = []
    for art in articles:
        media_line = ""
        if art.get("image_url"):
            media_line = (
                f'\n      <media:content url="{art["image_url"]}" medium="image"/>'
            )

        items.append(
            f"    <item>\n"
            f"      <title>{_cdata(art['title'])}</title>\n"
            f"      <link>{art['url']}</link>\n"
            f"      <guid isPermaLink=\"true\">{art['url']}</guid>\n"
            f"      <pubDate>{art['pub_date']}</pubDate>\n"
            f"      <category>{art['subcategory']}</category>\n"
            f"      <description>{_cdata(art.get('summary', ''))}</description>"
            f"{media_line}\n"
            f"    </item>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">\n'
        '  <channel>\n'
        '    <title>' + _cdata("কালবেলা মতামত") + '</title>\n'
        '    <link>https://www.kalbela.com/opinion</link>\n'
        '    <description>' + _cdata("কালবেলা মতামত বিভাগের সর্বশেষ নিবন্ধ") + '</description>\n'
        '    <language>bn</language>\n'
        f'    <lastBuildDate>{formatdate(usegmt=True)}</lastBuildDate>\n'
        '    <ttl>60</ttl>\n'
        + "\n".join(items) + "\n"
        "  </channel>\n"
        "</rss>\n"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"Fetching {OPINION_URL} …")
    try:
        resp = requests.get(OPINION_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[ERROR] fetch failed: {exc}", file=sys.stderr)
        sys.exit(1)

    fresh = scrape(resp.text)
    print(f"Scraped {len(fresh)} articles from live page.")

    # Merge with persisted history; fresh data wins on conflicts
    state = _load_state()
    for art in fresh:
        state[art["url"]] = art

    # Drop articles older than RETENTION_DAYS
    before = len(state)
    state = {
        url: art
        for url, art in state.items()
        if not _too_old(art.get("pub_date", ""))
    }
    pruned = before - len(state)
    if pruned:
        print(f"Pruned {pruned} article(s) older than {RETENTION_DAYS} days.")

    _save_state(state)

    ordered = sorted(state.values(), key=_sort_key, reverse=True)
    xml_out = _build_rss(ordered)
    Path(OUTPUT_XML).write_text(xml_out, encoding="utf-8")
    print(f"Feed written: {len(ordered)} articles → {OUTPUT_XML}")


if __name__ == "__main__":
    main()
