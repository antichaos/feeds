#!/usr/bin/env python3
"""
rss2json.py — fetch RSS/Atom feeds and write them as JSON for Tableau.

Standalone: Python 3.8+, standard library only. The output is read by the
Tableau REST API Connector (Response Format: JSON, JSON Path: $.items[*]).

Usage
    python3 rss2json.py                              # no arguments: uses feeds.json next to the script
    python3 rss2json.py -f feeds.json                # what the GitHub job runs (see rss.yml)
    python3 rss2json.py https://feeds.nos.nl/nosnieuwsalgemeen
    python3 rss2json.py -o out nos=https://feeds.nos.nl/nosnieuwsalgemeen tech=https://feeds.nos.nl/nosnieuwstech
    python3 rss2json.py -f feeds-catalog.json -c tableau          # one category from the catalog
    python3 rss2json.py -f feeds-catalog.json --only nos-algemeen,bbc-top

Writes <out>/<slug>.json per feed and <out>/all.json with everything combined
(--csv adds <out>/all.csv, the same rows as a flat table). See README.md for
how to publish them with GitHub Pages.
"""
import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

USER_AGENT = "rss2json/1.0 (+tableau test)"
# ISO-8601 with 'T', no offset: the only string shape Tableau's JDBC REST API driver types as TIMESTAMP.
TS_FMT = "%Y-%m-%dT%H:%M:%S"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "media": "http://search.yahoo.com/mrss/",
    "rss1": "http://purl.org/rss/1.0/",
    "georss": "http://www.georss.org/georss",
    "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#",
}
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)", re.I)


# ------------------------------------------------------------------ helpers

def strip_html(s):
    if not s:
        return None
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", s))).strip() or None


def to_utc(s):
    """RFC-822 (RSS) or ISO-8601 (Atom) date -> 'YYYY-MM-DDTHH:MM:SS' in UTC (see TS_FMT)."""
    if not s:
        return None
    s = s.strip()
    d = None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).strftime(TS_FMT)


def text(el, *paths):
    """First non-empty text among the given child paths (no path: the element's own text)."""
    if not paths:
        return el.text.strip() if el is not None and el.text and el.text.strip() else None
    for p in paths:
        node = el.find(p, NS)
        if node is not None and (node.text or "").strip():
            return node.text.strip()
    return None


def image_url(el, *html_blobs):
    """First image: enclosure, media:content, media:thumbnail, then <img> in HTML."""
    for enc in el.findall("enclosure"):
        if enc.get("url") and enc.get("type", "").startswith("image/"):
            return enc.get("url")
    for m in el.findall("media:content", NS) + el.findall("media:group/media:content", NS):
        if m.get("url") and (m.get("medium") == "image" or m.get("type", "").startswith("image/")):
            return m.get("url")
    for m in el.findall("media:thumbnail", NS) + el.findall("media:group/media:thumbnail", NS):
        if m.get("url"):
            return m.get("url")
    for blob in html_blobs:
        hit = _IMG_RE.search(blob or "")
        if hit:
            return hit.group(1)
    return None


def latlon(el):
    """georss:point 'lat lon' or geo:lat/geo:long -> (lat, lon) or (None, None)."""
    pt = text(el, "georss:point", "georss:where/georss:point")
    if pt:
        parts = pt.split()
        if len(parts) == 2:
            try:
                return float(parts[0]), float(parts[1])
            except ValueError:
                pass
    la, lo = text(el, "geo:lat"), text(el, "geo:long")
    try:
        return (float(la), float(lo)) if la and lo else (None, None)
    except ValueError:
        return None, None


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def slug_for(url):
    p = urlparse(url)
    base = (p.netloc + p.path).strip("/")
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:60] or "feed"


# ------------------------------------------------------------------ parsing

def parse_rss(channel, slug, now):
    meta = {
        "slug": slug,
        "title": text(channel, "title"),
        "link": text(channel, "link"),
    }
    items = []
    for it in channel.findall("item"):
        guid = text(it, "guid") or text(it, "link")
        title, link = text(it, "title"), text(it, "link")
        if not guid:
            guid = "sha1:" + hashlib.sha1(f"{title}|{text(it, 'pubDate')}".encode()).hexdigest()
        summary_html = text(it, "description")
        content_html = text(it, "content:encoded")
        cats = [c.text.strip() for c in it.findall("category") if (c.text or "").strip()]
        items.append({
            "feed_slug": slug,
            "guid": guid,
            "title": title,
            "link": link,
            "author": text(it, "author", "dc:creator"),
            "published": to_utc(text(it, "pubDate", "dc:date")),
            "updated": None,
            "categories": ", ".join(cats) or None,
            "image_url": image_url(it, summary_html, content_html),
            "lat": latlon(it)[0], "lon": latlon(it)[1],
            "summary": strip_html(summary_html),
            "summary_html": summary_html,
            "content": strip_html(content_html),
            "first_seen": now,
            "last_seen": now,
        })
    return meta, items


def parse_atom(feed, slug, now):
    def atom_link(el, rel="alternate"):
        for l in el.findall("atom:link", NS):
            if l.get("rel", "alternate") == rel and l.get("href"):
                return l.get("href")
        return None

    meta = {"slug": slug, "title": text(feed, "atom:title"), "link": atom_link(feed)}
    items = []
    for e in feed.findall("atom:entry", NS):
        title = text(e, "atom:title")
        link = atom_link(e)
        published = text(e, "atom:published", "atom:updated")
        guid = text(e, "atom:id") or link or "sha1:" + hashlib.sha1(f"{title}|{published}".encode()).hexdigest()
        summary_html = text(e, "atom:summary")
        content_html = text(e, "atom:content")
        cats = [c.get("term") for c in e.findall("atom:category", NS) if c.get("term")]
        items.append({
            "feed_slug": slug,
            "guid": guid,
            "title": title,
            "link": link,
            "author": text(e, "atom:author/atom:name"),
            "published": to_utc(published),
            "updated": to_utc(text(e, "atom:updated")),
            "categories": ", ".join(cats) or None,
            "image_url": image_url(e, summary_html, content_html),
            "lat": latlon(e)[0], "lon": latlon(e)[1],
            "summary": strip_html(summary_html) or strip_html(content_html),
            "summary_html": summary_html,
            "content": strip_html(content_html),
            "first_seen": now,
            "last_seen": now,
        })
    return meta, items


def parse_rdf(root, slug, now):
    """RSS 1.0 (RDF): <channel> and <item>s are siblings under rdf:RDF, in the rss1 namespace."""
    ch = root.find("rss1:channel", NS)
    meta = {"slug": slug, "title": text(ch, "rss1:title") if ch is not None else None,
            "link": text(ch, "rss1:link") if ch is not None else None}
    items = []
    for it in root.findall("rss1:item", NS):
        title, link = text(it, "rss1:title"), text(it, "rss1:link")
        published = text(it, "dc:date")
        summary_html = text(it, "rss1:description")
        content_html = text(it, "content:encoded")
        items.append({
            "feed_slug": slug,
            "guid": it.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about") or link
                    or "sha1:" + hashlib.sha1(f"{title}|{published}".encode()).hexdigest(),
            "title": title, "link": link,
            "author": text(it, "dc:creator"),
            "published": to_utc(published), "updated": None,
            "categories": ", ".join(s for s in (text(c) for c in it.findall("dc:subject", NS)) if s) or None,
            "image_url": image_url(it, summary_html, content_html),
            "lat": latlon(it)[0], "lon": latlon(it)[1],
            "summary": strip_html(summary_html), "summary_html": summary_html,
            "content": strip_html(content_html),
            "first_seen": now, "last_seen": now,
        })
    return meta, items


def parse_feed(xml_bytes, slug, url, now):
    root = ET.fromstring(xml_bytes.lstrip())  # some feeds emit whitespace before <?xml
    tag = root.tag.split("}")[-1].lower()
    if tag == "rss":
        meta, items = parse_rss(root.find("channel"), slug, now)
    elif tag == "feed":
        meta, items = parse_atom(root, slug, now)
    elif tag == "rdf":
        meta, items = parse_rdf(root, slug, now)
    else:
        raise ValueError(f"unrecognised feed root <{root.tag}>")
    meta["url"] = url
    return meta, items


# --------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("feeds", nargs="*", help="feed URL, or slug=URL")
    ap.add_argument("-f", "--feeds-file", help="JSON file with {\"feeds\": [{\"slug\":..., \"url\":...}]}")
    ap.add_argument("-c", "--category", help="with -f: only feeds whose category contains this text (case-insensitive), e.g. -c tableau")
    ap.add_argument("--only", help="with -f: comma-separated slugs to fetch, e.g. --only nos-algemeen,bbc-top")
    ap.add_argument("-o", "--out", default="out", help="output directory (default: ./out)")
    ap.add_argument("--csv", action="store_true", help="also write all.csv (one row per item, all feeds)")
    ap.add_argument("--pretty", action="store_true", help="indent the JSON (bigger files, easier to read)")
    args = ap.parse_args(argv)

    feeds = []
    if args.feeds_file:
        entries = json.loads(Path(args.feeds_file).read_text())["feeds"]
        if args.category:
            entries = [f for f in entries if args.category.lower() in f.get("category", "").lower()]
        if args.only:
            keep = {x.strip() for x in args.only.split(",")}
            entries = [f for f in entries if f["slug"] in keep]
        feeds += [(f["slug"], f["url"]) for f in entries]
    for spec in args.feeds:
        slug, sep, url = spec.partition("=")
        feeds.append((slug, url) if sep else (slug_for(spec), spec))
    if not feeds and not args.feeds_file:
        # No arguments: use a feeds.json next to this script (or in the current
        # directory) so novices can just run `python3 rss2json.py`.
        for cand in (Path(__file__).resolve().parent / "feeds.json", Path("feeds.json")):
            if cand.is_file():
                print(f"[info] no arguments given, using {cand}")
                feeds += [(f["slug"], f["url"]) for f in json.loads(cand.read_text())["feeds"]]
                break
    if not feeds:
        ap.error("give at least one feed URL or --feeds-file (or put a feeds.json next to this script)")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).strftime(TS_FMT)
    dump = dict(ensure_ascii=False, indent=2 if args.pretty else None)

    all_items, all_meta, failures = [], [], 0
    for slug, url in feeds:
        try:
            meta, items = parse_feed(fetch(url), slug, url, now)
        except Exception as e:  # keep going; report at the end
            print(f"[FAIL] {slug}: {e}", file=sys.stderr)
            failures += 1
            continue
        items.sort(key=lambda i: i["published"] or "", reverse=True)
        (out / f"{slug}.json").write_text(json.dumps({
            "feed": meta, "generated_at": now, "item_count": len(items), "items": items,
        }, **dump), encoding="utf-8")
        print(f"[ ok ] {slug}: {len(items)} items -> {out / f'{slug}.json'}")
        all_items += items
        all_meta.append(meta)

    all_items.sort(key=lambda i: i["published"] or "", reverse=True)
    (out / "all.json").write_text(json.dumps({
        "feeds": all_meta, "generated_at": now, "item_count": len(all_items), "items": all_items,
    }, **dump), encoding="utf-8")
    print(f"[ ok ] all.json: {len(all_items)} items from {len(all_meta)} feeds")
    if args.csv and all_items:
        cols = list(all_items[0].keys())
        with (out / "all.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(all_items)
        print(f"[ ok ] all.csv: {len(all_items)} rows, {len(cols)} columns")
    print("\nTableau REST API Connector:  URL = <your host>/all.json   Response Format = JSON   JSON Path = $.items[*]")
    if failures:
        print(f"[warn] {failures} of {len(feeds)} feeds failed (see [FAIL] lines above)", file=sys.stderr)
    # Fail the run only when nothing could be fetched, so one dead feed does
    # not stop the GitHub job from publishing the others.
    return 1 if failures and not all_meta else 0


if __name__ == "__main__":
    sys.exit(main())
