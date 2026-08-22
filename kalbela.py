#!/usr/bin/env python3
"""
kalbela.py
----------
Scrapes kalbela.com/opinion via FlareSolverr and generates
an Inoreader-compatible RSS 2.0 feed with RFC 2822 pub dates.

Date strategy
    Every article image filename encodes a UTC Unix timestamp:
        /assets/news_photos/2026/08/20/image_320468_1787230722.webp
    Falls back to YYYY/MM/DD path (midnight UTC), then current time.

State
    Persisted in feed_state.json — articles stay in feed for
    RETENTION_DAYS after dropping off the live page.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Config ───────────────────────────────────────────────────────────────────

BASE_URL         = "https://www.kalbela.com"
OPINION_URL      = f"{BASE_URL}/opinion"
OUTPUT_XML       = "opinion.xml"
STATE_FILE       = "feed_state.json"
RETENTION_DAYS   = 30
FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "http://localhost:8191/v1")

# ── Date helpers ──────────────────────────────────────────────────────────────

_RE_IMG_TS   = re.compile(r"/image[_-]\d+[_-](\d{9,11})\.")
_RE_IMG_PATH = re.compile(r"/news_photos/(\d{4})/(\d{2})/(\d{2})/")
_LAZY_MARKER = "lazy-logo.png"


def _pub_date_from_image(img_src: str) -> str:
    m = _RE_IMG_TS.search(img_src)
    if m:
        return formatdate(int(m.group(1)), usegmt=True)
    m = _RE_IMG_PATH.search(img_src)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return formatdate(datetime(y, mo, d, tzinfo=timezone.utc).timestamp(), usegmt=True)
    return formatdate(usegmt=True)


# ── Scraper ───────────────────────────────────────────────────────────────────

_OPINION_RE = re.compile(
    r"https://www\.kalbela\.com/opinion/[A-Za-z0-9_-]+/\d+$"
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _abs(href: str) -> str:
    """Make relative hrefs absolute."""
    if href.startswith("/"):
        return BASE_URL + href
    return href


def scrape(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    articles: list[dict] = []
    seen: set[str] = set()

    # ── Debug counts ───────────────────────────────────────────────────────
    all_a   = soup.find_all("a", href=True)
    all_h5  = soup.find_all("h5", class_="titleShow")
    all_img = soup.find_all("img", class_="news_img")
    print(f"[DEBUG] a[href]={len(all_a)}  h5.titleShow={len(all_h5)}  img.news_img={len(all_img)}")

    opinion_hrefs = [_abs(a["href"]) for a in all_a if "opinion" in a.get("href", "")]
    print(f"[DEBUG] hrefs containing 'opinion' ({len(opinion_hrefs)}): {opinion_hrefs[:6]}")
    # ──────────────────────────────────────────────────────────────────────

    for a_tag in soup.find_all("a", href=True):
        href = _abs((a_tag.get("href") or "").strip())

        if not _OPINION_RE.match(href):
            continue

        # Accept this <a> only if the title heading lives somewhere inside it.
        # Works regardless of how many levels deep h5 is nested.
        h5 = a_tag.find("h5", class_="titleShow")
        if not h5:
            continue

        if href in seen:
            continue
        seen.add(href)

        # Walk up to card container (first ancestor holding an img.news_img)
        card = a_tag
        for _ in range(12):
            card = card.parent
            if card is None:
                break
            if card.find("img", class_="news_img"):
                break

        if card is None:
            continue

        # Image
        img_tag = card.find("img", class_="news_img")
        raw_src = img_url = ""
        if img_tag:
            raw_src = img_tag.get("src") or img_tag.get("data-original") or ""
            if _LAZY_MARKER in raw_src:
                raw_src = img_tag.get("data-original") or ""
            if raw_src:
                img_url = raw_src if raw_src.startswith("http") else BASE_URL + raw_src

        # Summary
        summary_div = card.find("div", class_="summery")
        summary = _clean(summary_div.get_text(" ", strip=True))[:800] if summary_div else ""

        # Subcategory from URL path
        subcategory = href.rstrip("/").split("/")[-2]

        articles.append({
            "url":         href,
            "title":       _clean(h5.get_text(" ", strip=True)),
            "summary":     summary,
            "image_url":   img_url,
            "pub_date":    _pub_date_from_image(raw_src),
            "subcategory": subcategory,
        })

    return articles


# ── FlareSolverr fetch ────────────────────────────────────────────────────────

def _fetch_html(url: str) -> str:
    payload = {"cmd": "request.get", "url": url, "maxTimeout": 60000}
    resp = requests.post(FLARESOLVERR_URL, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr error: {data.get('message', data)}")
    return data["solution"]["response"]


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
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _too_old(rfc2822: str) -> bool:
    try:
        return parsedate_to_datetime(rfc2822) < datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    except Exception:
        return False


def _sort_key(art: dict) -> datetime:
    try:
        return parsedate_to_datetime(art["pub_date"])
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


# ── RSS builder ───────────────────────────────────────────────────────────────

def _cdata(text: str) -> str:
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _build_rss(articles: list[dict]) -> str:
    items: list[str] = []
    for art in articles:
        media_line = ""
        if art.get("image_url"):
            media_line = f'\n      <media:content url="{art["image_url"]}" medium="image"/>'

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
    print(f"Fetching {OPINION_URL} via FlareSolverr …")
    try:
        html = _fetch_html(OPINION_URL)
    except Exception as exc:
        print(f"[ERROR] fetch failed: {exc}", file=sys.stderr)
        sys.exit(1)

    fresh = scrape(html)
    print(f"Scraped {len(fresh)} articles from live page.")

    state = _load_state()
    for art in fresh:
        state[art["url"]] = art

    before = len(state)
    state = {u: a for u, a in state.items() if not _too_old(a.get("pub_date", ""))}
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
