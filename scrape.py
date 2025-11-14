import feedparser
import xml.etree.ElementTree as ET
import os
from datetime import datetime
import calendar
import email.utils

# Original and new source
SOURCES = [
    "https://www.kalbela.com/rss/popular-rss.xml",
    "https://evilgodfahim.github.io/kal/articles.xml",

"https://www.prothomalo.com/feed/"
]

FILES = {
    "opinion": "opinion.xml",
    "world": "world.xml",
    "daily": "daily.xml"
}

def load_existing(path):
    if not os.path.exists(path):
        root = ET.Element("rss", version="2.0")
        ET.SubElement(root, "channel")
        return root
    return ET.parse(path).getroot()

def format_pubdate(dt):
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")

def parse_struct_time(st):
    return datetime.utcfromtimestamp(calendar.timegm(st))

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
            return email.utils.parsedate_to_datetime(ps).replace(tzinfo=None)
        except Exception:
            pass
    return datetime.utcnow()

def get_item_pubdt(item):
    txt = item.findtext("pubDate")
    if not txt:
        return datetime.min
    try:
        return email.utils.parsedate_to_datetime(txt).replace(tzinfo=None)
    except Exception:
        try:
            return datetime.strptime(txt, "%a, %d %b %Y %H:%M:%S GMT")
        except Exception:
            return datetime.min

def merge_update_feed(root, entries):
    channel = root.find("channel")
    existing_map = {}
    for item in channel.findall("item"):
        link_text = item.findtext("link")
        if link_text:
            existing_map[link_text] = item

    for entry in entries:
        link = getattr(entry, "link", None) or getattr(entry, "id", None)
        if not link:
            continue
        link = link.strip()
        incoming_dt = get_entry_pubdt(entry)

        if link in existing_map:
            item = existing_map[link]
            existing_dt = get_item_pubdt(item)
            if incoming_dt > existing_dt:
                t = item.find("title")
                if t is None:
                    t = ET.SubElement(item, "title")
                t.text = getattr(entry, "title", t.text)

                pd = item.find("pubDate")
                if pd is None:
                    pd = ET.SubElement(item, "pubDate")
                pd.text = getattr(entry, "published", format_pubdate(incoming_dt))

                g = item.find("guid")
                if g is None:
                    g = ET.SubElement(item, "guid", isPermaLink="false")
                g.text = link

                channel.remove(item)
                channel.insert(0, item)
        else:
            item = ET.Element("item")
            ET.SubElement(item, "title").text = getattr(entry, "title", "")
            ET.SubElement(item, "link").text = link
            ET.SubElement(item, "pubDate").text = getattr(entry, "published", format_pubdate(incoming_dt))
            ET.SubElement(item, "guid", isPermaLink="false").text = link
            channel.insert(0, item)
            existing_map[link] = item

    all_items = channel.findall("item")
    if len(all_items) > 500:
        for extra in all_items[500:]:
            channel.remove(extra)

# Load and merge entries from all sources
all_entries = []
for src in SOURCES:
    feed = feedparser.parse(src)
    all_entries.extend(feed.entries)

# opinion
op_root = load_existing(FILES["opinion"])
op_entries = [
    e for e in all_entries
    if any(x in ((getattr(e, "link", None) or getattr(e, "id", None) or "").strip())
           for x in ["/opinion/", "/joto-mot-toto-path/"])
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
