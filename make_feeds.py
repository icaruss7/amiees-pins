#!/usr/bin/env python3
"""
Build Pinterest RSS 2.0 feeds from pins.csv.

One feed per board, because Pinterest publishes each feed to exactly one
board. An item only appears once its release_at has passed, which is how the
pace is controlled: Pinterest itself has no scheduling cap and will publish
whatever it finds, up to 200 a day, so the feed is the throttle.

Idempotent. Run it as often as you like; it only ever rewrites the XML.
"""

import csv
import os
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "pins.csv")
FEED_DIR = os.path.join(HERE, "feeds")

SITE = "https://amiees.com"
MAX_ITEMS = 150          # keep the file small; Pinterest dedupes on guid
CHANNEL_TTL = 60

RSS_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">\n'
)


def slug(board):
    return re.sub(r"[^a-z0-9]+", "-", board.lower()).strip("-")


def parse(ts):
    """pins.csv stores UTC as 'YYYY-MM-DD HH:MM:SS'."""
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def item_xml(row):
    released = parse(row["release_at"])
    return (
        "    <item>\n"
        f"      <title>{escape(row['title'])}</title>\n"
        f"      <link>{escape(row['link'])}</link>\n"
        f"      <description>{escape(row['description'])}</description>\n"
        f"      <guid isPermaLink=\"false\">{escape(row['guid'])}</guid>\n"
        f"      <pubDate>{format_datetime(released)}</pubDate>\n"
        f"      <enclosure url=\"{escape(row['image'])}\" length=\"0\" type=\"image/jpeg\"/>\n"
        f"      <media:content url=\"{escape(row['image'])}\" medium=\"image\"/>\n"
        "    </item>\n"
    )


def build(board, rows, now):
    live = [r for r in rows
            if r["status"] == "queued" and parse(r["release_at"]) <= now]
    live.sort(key=lambda r: r["release_at"])
    live = live[-MAX_ITEMS:]

    out = [RSS_HEAD, "  <channel>\n",
           f"    <title>Amiees {escape(board)}</title>\n",
           f"    <link>{SITE}</link>\n",
           f"    <description>Nature inspired jewelry from Amiees.</description>\n",
           "    <language>en-us</language>\n",
           f"    <lastBuildDate>{format_datetime(now)}</lastBuildDate>\n",
           f"    <ttl>{CHANNEL_TTL}</ttl>\n"]
    out += [item_xml(r) for r in live]
    out += ["  </channel>\n", "</rss>\n"]

    path = os.path.join(FEED_DIR, f"{slug(board)}.xml")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(out))
    return path, len(live)


def main():
    now = datetime.now(timezone.utc)
    with open(MANIFEST, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    seen = set()
    for r in rows:
        if r["guid"] in seen:
            raise SystemExit(f"duplicate guid in pins.csv: {r['guid']}")
        seen.add(r["guid"])
        if not r["link"].startswith(SITE):
            raise SystemExit(f"link is not on the claimed domain: {r['link']}")

    os.makedirs(FEED_DIR, exist_ok=True)
    boards = sorted({r["board"] for r in rows})
    for board in boards:
        path, n = build(board, [r for r in rows if r["board"] == board], now)
        print(f"{os.path.basename(path):<28} {n:>3} live of "
              f"{sum(1 for r in rows if r['board'] == board and r['status'] == 'queued')} queued")

    pending = [r for r in rows
               if r["status"] == "queued" and parse(r["release_at"]) > now]
    if pending:
        nxt = min(pending, key=lambda r: r["release_at"])
        print(f"\n{len(pending)} still held back. Next release {nxt['release_at']} UTC.")
    else:
        print("\nEverything queued has been released.")


if __name__ == "__main__":
    main()
