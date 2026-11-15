#!/usr/bin/env python3
"""Discover fresh Chelsea stories from direct club/league pages and specialist RSS.

This is discovery only: it never treats discovery metadata as verified evidence,
never writes an article, and never publishes. The normal evidence processor remains
the only authority for drafting and auto-scheduling.
"""
from __future__ import annotations

import json
import logging
import re
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

import collect_news as c

ROOT = Path(__file__).resolve().parent
CANDIDATES = ROOT / "news_candidates.json"
HISTORY = ROOT / "seen_stories.json"
REPORT = ROOT / "collection_report.json"
MAX_LINKS_PER_PAGE = 18
MAX_SITEMAP_FILES = 8
MAX_SITEMAP_ARTICLES = 30
ROBOTS_URL = "https://www.chelseafc.com/robots.txt"
SITEMAP_FALLBACKS = ["https://www.chelseafc.com/sitemap_index.xml", "https://www.chelseafc.com/sitemap.xml"]
PINNED_OFFICIAL_SOURCES = [
    {
        "name": "Chelsea FC Official",
        "url": "https://www.chelseafc.com/en/news/article/wieke-kaptein-we-can-take-confidence-from-man-city-victory",
        "story_key": "wieke_kaptein_reaction",
        "required_title_terms": ["wieke kaptein", "man city victory"],
        "required_body_terms": ["paris saint-germain", "sandy baltimore"],
    },
    {
        "name": "Chelsea FC Official",
        "url": "https://www.chelseafc.com/en/news/article/chelseas-player-of-the-month-for-october-2026-revealed",
        "story_key": "morgan_rogers_award",
        "required_title_terms": ["player of the month", "october"],
        "required_body_terms": ["morgan rogers"],
    },
]
# The official latest-news landing page is a discovery index only. Every linked
# article still needs its own accessible page and publication timestamp.
LANDING_PAGES = [
    {"name": "Chelsea FC official homepage news index", "url": "https://www.chelseafc.com/en"},
    {"name": "Chelsea FC official news index", "url": "https://www.chelseafc.com/en/news"},
];
SPECIALIST_FEEDS = [
    {"name": "We Ain't Got No History", "url": "https://weaintgotnohistory.sbnation.com/rss/current.xml"},
    {"name": "The False 9", "url": "https://thefalse9.com/feed/"},
]
ALLOWED_OFFICIAL = {"chelseafc.com", "wslfootball.com", "premierleague.com", "uefa.com", "thefa.com", "englandfootball.com"}
c.OFFICIAL.add("englandfootball.com")


def parse_iso(value):
    if not value:
        return None
    raw = str(value).strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def jsonld_articles(value):
    if isinstance(value, list):
        for item in value:
            yield from jsonld_articles(item)
    elif isinstance(value, dict):
        if any(k in value for k in ("datePublished", "publishedAt", "publishedDate", "publishDate", "publicationDate", "headline", "articleBody")):
            yield value
        graph = value.get("@graph")
        if graph:
            yield from jsonld_articles(graph)
        for key, child in value.items():
            if key in ("mainEntity", "mainEntityOfPage", "itemListElement", "article", "content", "data", "props", "pageProps", "initialState") and isinstance(child, (dict, list)):
                yield from jsonld_articles(child)

def article_metadata(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    for selector in ('meta[property="og:title"]', 'meta[name="twitter:title"]'):
        node = soup.select_one(selector)
        if node and node.get("content"):
            title = c.textval(node["content"])
            break
    if not title:
        node = soup.find("h1")
        title = c.textval(node.get_text(" ", strip=True)) if node else ""
    description = ""
    for selector in ('meta[name="description"]', 'meta[property="og:description"]'):
        node = soup.select_one(selector)
        if node and node.get("content"):
            description = c.textval(node["content"])
            break
    if not title and soup.title:
        title = c.textval(soup.title.get_text(" ", strip=True))
    published = None
    # Chelsea and its publishing platform may expose the original publication
    # date under different standard metadata names. Never substitute modified
    # timestamps or sitemap lastmod values for publication time.
    date_selectors = (
        'meta[property="article:published_time"]',
        'meta[name="article:published_time"]',
        'meta[itemprop="datePublished"]',
        'meta[property="og:published_time"]',
        'meta[name="date"]',
        'meta[name="pubdate"]',
        'meta[name="publish-date"]',
        'meta[name="parsely-pub-date"]',
        'meta[name="dc.date.issued"]',
        'meta[name="DC.date.issued"]',
    )
    for selector in date_selectors:
        node = soup.select_one(selector)
        if node:
            published = parse_iso(node.get("content") or node.get("datetime") or node.get_text(" ", strip=True))
            if published:
                break
    if not published:
        for node in soup.select('time[itemprop="datePublished"], time[datetime]'):
            published = parse_iso(node.get("datetime") or node.get_text(" ", strip=True))
            if published:
                break
    if not published:
        for node in soup.find_all("script"):
            raw = node.string or node.get_text()
            if not raw:
                continue
            payload = None
            try:
                payload = json.loads(raw)
            except Exception:
                pass
            if payload is not None:
                for item in jsonld_articles(payload):
                    published = parse_iso(
                        item.get("datePublished") or item.get("publishedAt") or
                        item.get("publishedDate") or item.get("publishDate") or
                        item.get("publicationDate")
                    )
                    if published:
                        title = title or c.textval(item.get("headline") or item.get("name"))
                        description = description or c.textval(item.get("description"))
                        break
            # Some CMS pages embed article metadata in a JavaScript state
            # assignment rather than JSON-LD. Accept only explicitly named
            # publication-date fields, never generic update/creation dates.
            if not published:
                match = re.search(
                    r"""(?i)["']?(?:datePublished|publishedAt|publishedDate|publishDate|publicationDate)["']?\s*[:=]\s*["']([^"']{8,50})""",
                    raw
                )
                if match:
                    published = parse_iso(match.group(1))
            if published:
                break
    return title, description, published


def sitemap_entries(xml_text):
    """Extract sitemap locations with lastmod as a discovery-only sort hint."""
    root = ET.fromstring(xml_text)
    kind = root.tag.rsplit("}", 1)[-1].lower()
    entries = []
    for node in list(root):
        record = {}
        for child in list(node):
            key = child.tag.rsplit("}", 1)[-1].lower()
            if key in ("loc", "lastmod") and child.text:
                record[key] = child.text.strip()
        if record.get("loc"):
            entries.append(record)
    return kind, entries


def sitemap_locations(xml_text):
    """Compatibility helper used by regression tests."""
    kind, entries = sitemap_entries(xml_text)
    return kind, [entry["loc"] for entry in entries]


def pinned_story_matches(source, title, page_text):
    title_l = c.textval(title).lower()
    body_l = c.textval(page_text).lower()
    return (all(term in title_l for term in source.get("required_title_terms", []))
            and all(term in body_l for term in source.get("required_body_terms", [])))


def is_official_article_url(url):
    parsed = urlparse(url or "")
    return (parsed.scheme in {"http", "https"}
            and c.matches(c.domain(url), {"chelseafc.com"})
            and "/en/news/article/" in parsed.path)


def run_self_tests():
    # The live Kaptein article must be recognisable from its own headline and body,
    # while unrelated pages cannot pass the pinned-source guard.
    kaptein = PINNED_OFFICIAL_SOURCES[0]
    assert pinned_story_matches(kaptein,
        "Wieke Kaptein: We can take confidence from Man City victory",
        "Kaptein discusses Sandy Baltimore and the upcoming Paris Saint-Germain match.")
    assert not pinned_story_matches(kaptein, "Chelsea latest news",
        "An unrelated club announcement with no matching article details.")
    assert is_official_article_url("https://www.chelseafc.com/en/news/article/example")
    assert not is_official_article_url("https://news.google.com/rss/articles/example")
    assert not is_official_article_url("https://chelseafc.com.example.org/en/news/article/example")
    assert all("published_fallback" not in source for source in PINNED_OFFICIAL_SOURCES)
    assert parse_iso("Sun, 15 Nov 2026 13:00:00 GMT") == datetime(2026, 11, 15, 13, 0, tzinfo=timezone.utc)
    title, summary, published = article_metadata(
        '<html><head><meta property="og:title" content="Wieke Kaptein: We can take confidence">'
        '<meta name="pubdate" content="Sun, 15 Nov 2026 13:00:00 GMT"></head>'
        '<body><h1>Headline</h1><p>Article body</p></body></html>', "https://www.chelseafc.com/en/news/article/test")
    assert title == "Wieke Kaptein: We can take confidence" and published == datetime(2026, 11, 15, 13, 0, tzinfo=timezone.utc)
    ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
    urlset = f'<urlset xmlns="{ns}"><url><loc>https://www.chelseafc.com/en/news/article/test-story</loc></url></urlset>'
    kind, locations = sitemap_locations(urlset)
    assert kind == "urlset" and locations == ["https://www.chelseafc.com/en/news/article/test-story"]
    index = f'<sitemapindex xmlns="{ns}"><sitemap><loc>https://www.chelseafc.com/news-sitemap.xml</loc><lastmod>2026-11-15</lastmod></sitemap></sitemapindex>'
    kind, locations = sitemap_locations(index)
    assert kind == "sitemapindex" and locations == ["https://www.chelseafc.com/news-sitemap.xml"]
    kind, records = sitemap_entries(index)
    assert kind == "sitemapindex" and records[0]["lastmod"] == "2026-11-15"
    print("PASS: sitemap URL-set/index parsing and lastmod extraction; article dates remain independently required.")


def discover_official_sitemaps(session):
    """Discover current Chelsea article URLs from the club's own sitemap.

    Sitemap timestamps are discovery hints only. Every article must still return
    an accessible page with its own published date before it becomes a candidate.
    """
    report = {"source": "Chelsea FC robots.txt and official sitemap", "url": ROBOTS_URL,
              "status": "failed", "entries_received": 0,
              "observations_collected": 0, "article_fetches": 0,
              "rejected_undated_or_stale": 0, "sitemap_urls_found": 0, "sitemap_candidates": [], "error": None}
    observations, sitemap_files, article_urls = [], [], []
    try:
        sitemap_candidates = []
        try:
            robots = session.get(ROBOTS_URL, timeout=c.TIMEOUT)
            robots.raise_for_status()
            sitemap_candidates.extend(
                line.split(":", 1)[1].strip()
                for line in robots.text.splitlines()
                if line.strip().lower().startswith("sitemap:") and ":" in line
            )
        except requests.RequestException as exc:
            logging.info("Chelsea robots.txt unavailable (%s); trying standard sitemap paths", type(exc).__name__)
        sitemap_candidates.extend(SITEMAP_FALLBACKS)
        sitemap_candidates = list(dict.fromkeys(
            u for u in sitemap_candidates if c.matches(c.domain(u), ALLOWED_OFFICIAL)
        ))
        report["sitemap_urls_found"] = len(sitemap_candidates)
        report["sitemap_candidates"] = sitemap_candidates
        root_documents, last_error = [], None
        for sitemap_url in sitemap_candidates:
            try:
                response = session.get(sitemap_url, timeout=c.TIMEOUT)
                response.raise_for_status()
                root_kind, root_records = sitemap_entries(response.text)
                if root_kind in ("urlset", "sitemapindex"):
                    root_documents.append((sitemap_url, root_kind, root_records))
            except Exception as exc:
                last_error = exc
                logging.info("Official sitemap endpoint skipped (%s): %s", sitemap_url, type(exc).__name__)
        if not root_documents:
            raise RuntimeError("No working official sitemap URL found: " + str(last_error or "not declared"))

        article_records, child_sitemaps_seen = [], 0
        for root_url, root_kind, root_records in root_documents:
            report["url"] = root_url
            if root_kind == "urlset":
                article_records.extend(r for r in root_records
                                       if "/en/news/article/" in urlparse(r.get("loc", "")).path)
                continue
            sitemap_files = [r for r in root_records
                             if c.matches(c.domain(r.get("loc", "")), ALLOWED_OFFICIAL)]
            sitemap_files.sort(key=lambda r: r.get("lastmod", ""), reverse=True)
            sitemap_files.sort(key=lambda r: 0 if re.search(r"news|article|content", urlparse(r["loc"]).path, re.I) else 1)
            for sitemap_record in sitemap_files:
                if child_sitemaps_seen >= MAX_SITEMAP_FILES:
                    break
                child_sitemaps_seen += 1
                child_url = sitemap_record["loc"]
                try:
                    child = session.get(child_url, timeout=c.TIMEOUT)
                    child.raise_for_status()
                    child_kind, child_records = sitemap_entries(child.text)
                    if child_kind == "urlset":
                        article_records.extend(r for r in child_records
                                               if "/en/news/article/" in urlparse(r.get("loc", "")).path)
                except Exception as exc:
                    logging.info("Official child sitemap skipped (%s): %s", child_url, type(exc).__name__)
        article_records.sort(key=lambda r: r.get("lastmod", ""), reverse=True)
        article_urls = [r["loc"] for r in article_records]

        # Keep only official Chelsea article URLs; do not use arbitrary sitemap links.
        article_urls = list(dict.fromkeys(
            c.normalize_url(u) for u in article_urls
            if c.matches(c.domain(u), ALLOWED_OFFICIAL)
            and "/en/news/article/" in urlparse(u).path
        ))[:MAX_SITEMAP_ARTICLES]
        report["entries_received"] = len(article_urls)

        cutoff = c.now() - timedelta(hours=c.MAX_AGE_HOURS)
        for url in article_urls:
            try:
                article = session.get(url, timeout=c.TIMEOUT, allow_redirects=True)
                article.raise_for_status()
                final_url = c.normalize_url(article.url)
                if not c.matches(c.domain(final_url), ALLOWED_OFFICIAL):
                    continue
                title, summary, published = article_metadata(article.text, final_url)
                if not title or not published or published < cutoff or published > c.now() + timedelta(hours=12):
                    report["rejected_undated_or_stale"] += 1
                    continue
                page_text = BeautifulSoup(article.text, "html.parser").get_text(" ", strip=True)
                if len(page_text) < 180:
                    continue
                item = c.observation(title, summary, final_url,
                                     "Chelsea FC official sitemap", "Chelsea Football Club", published)
                if item:
                    item["publisher_type"] = "official_domain"
                    observations.append(item)
            except requests.RequestException as exc:
                logging.info("Official sitemap article skipped (%s): %s", url, type(exc).__name__)
            finally:
                report["article_fetches"] += 1

        report["observations_collected"] = len(observations)
        report["status"] = "success" if observations else "empty"
        if not observations:
            report["error"] = "Sitemap returned no accessible, dated Chelsea articles inside the freshness window"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        logging.warning("Chelsea official sitemap discovery: %s", report["error"])
    return observations, report


def discover_official_pages(session):
    observations, report = [], []
    cutoff = c.now() - timedelta(hours=c.MAX_AGE_HOURS)
    for page in LANDING_PAGES:
        row = {"source": page["name"], "url": page["url"], "status": "failed",
               "entries_received": 0, "observations_collected": 0, "error": None}
        try:
            response = session.get(page["url"], timeout=c.TIMEOUT)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            links, seen = [], set()
            for anchor in soup.find_all("a", href=True):
                url = c.normalize_url(urljoin(response.url, anchor["href"]))
                host = c.domain(url)
                if not url or not c.matches(host, ALLOWED_OFFICIAL):
                    continue
                path = urlparse(url).path
                if "/news/article/" not in path or url in seen:
                    continue
                seen.add(url)
                links.append(url)
            row["entries_received"] = len(links)
            for url in links[:MAX_LINKS_PER_PAGE]:
                try:
                    article = session.get(url, timeout=c.TIMEOUT)
                    article.raise_for_status()
                    title, summary, published = article_metadata(article.text, article.url)
                    if not published or published < cutoff or published > c.now() + timedelta(hours=12):
                        continue
                    item = c.observation(title, summary, article.url, page["name"], "Official club website", published)
                    if item:
                        observations.append(item)
                except requests.RequestException as exc:
                    logging.info("Official article fetch skipped (%s): %s", url, type(exc).__name__)
            row["observations_collected"] = sum(x["source_feed"] == page["name"] for x in observations)
            row["status"] = "success" if row["observations_collected"] else "empty"
            if row["status"] == "empty":
                row["error"] = "No dated, fresh Chelsea article pages could be extracted"
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
            logging.warning("%s: %s", page["name"], row["error"])
        report.append(row)
    return observations, report

def discover_pinned_official_story(session, history):
    """Fetch known direct Chelsea announcements; page metadata, not hardcoded dates, gates freshness."""
    observations = []
    report = {"source": "Pinned Chelsea official announcements",
        "url": "direct Chelsea FC article pages", "status": "failed",
        "entries_received": len(PINNED_OFFICIAL_SOURCES),
        "observations_collected": 0, "article_checks": [], "error": None}
    cutoff = c.now() - timedelta(hours=c.MAX_AGE_HOURS)
    for source in PINNED_OFFICIAL_SOURCES:
        check = {"story_key": source["story_key"], "url": source["url"], "status": "failed"}
        try:
            response = session.get(source["url"], timeout=c.TIMEOUT, allow_redirects=True)
            response.raise_for_status()
            final_url = c.normalize_url(response.url)
            if not c.matches(c.domain(final_url), ALLOWED_OFFICIAL):
                check["error"] = "Redirect left the official-source allowlist"
                report["article_checks"].append(check)
                continue
            title, summary, published = article_metadata(response.text, final_url)
            soup = BeautifulSoup(response.text, "html.parser")
            page_text = soup.get_text(" ", strip=True)
            check["http_status"] = response.status_code
            check["html_chars"] = len(response.text or "")
            check["page_text_chars"] = len(page_text)
            check["html_title"] = c.textval(soup.title.get_text(" ", strip=True)) if soup.title else ""
            check["h1"] = c.textval(soup.find("h1").get_text(" ", strip=True)) if soup.find("h1") else ""
            check["publication_date_fields"] = re.findall(
                r"""(?i)(?:datePublished|publishedAt|publishedDate|publishDate|publicationDate)["']?\s*[:=]\s*["']([^"']{8,50})""",
                response.text
            )[:8]
            if not title or not published:
                check["status"] = "rejected"
                check["error"] = "Official article page did not expose a verifiable title and publication timestamp"
            elif published < cutoff or published > c.now() + timedelta(hours=12):
                check["status"] = "stale_or_future"
                check["published"] = published.isoformat()
            elif not pinned_story_matches(source, title, page_text):
                check["status"] = "rejected"
                check["error"] = "Official page did not match the configured headline/body identity checks"
            else:
                item = c.observation(title, summary, final_url, source["name"], "Chelsea Football Club", published)
                if item:
                    item["publisher_type"] = "official_domain"
                    item["official_source_key"] = source["story_key"]
                    observations.append(item)
                    check["status"] = "success"
                    check["published"] = published.isoformat()
                    check["title"] = title
                else:
                    check["status"] = "rejected"
                    check["error"] = "Official article did not pass the collector relevance/freshness gate"
        except Exception as exc:
            check["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        report["article_checks"].append(check)

    groups = c.cluster(observations)
    terminal = {"draft_created_unpublished", "rejected_quality_gate", "scheduled", "published", "archived"}
    events = history.get("events", {}) if isinstance(history, dict) else {}
    pending = []
    for group in groups:
        if events.get(group.get("event_id"), {}).get("status") in terminal:
            continue
        c.score(group)
        group["verification_status"] = "not_started"
        group["verification_checklist"] = c.verification_checklist(group)
        pending.append(group)
    report["observations_collected"] = len(observations)
    report["status"] = "success" if observations else "empty"
    if not observations:
        report["error"] = "No pinned official Chelsea article passed direct-page identity and 24-hour publication checks"
    return pending, report

def discover_google_news_official(session):
    """Use Google News RSS only as a discovery index for direct Chelsea articles.

    Google News supplies a publication-time hint; it is not counted as evidence.
    Every result must resolve to a first-party Chelsea article URL, and the normal
    processor must still fetch and verify the article body and all material claims.
    """
    from urllib.parse import quote_plus
    queries = [
        'site:chelseafc.com/en/news/article Chelsea',
        'site:chelseafc.com/en/news/article "Wieke Kaptein"',
    ]
    report = {"source": "Google News first-party Chelsea article discovery",
              "url": "https://news.google.com/rss/", "status": "failed",
              "entries_received": 0, "observations_collected": 0,
              "rejected_non_official": 0, "query_reports": [], "error": None}
    observations, seen = [], set()
    cutoff = c.now() - timedelta(hours=c.MAX_AGE_HOURS)
    for query in queries:
        row = {"query": query, "entries_received": 0,
               "observations_collected": 0, "status": "failed",
               "sample_entries": [], "rejected_stale": 0,
               "rejected_source_mismatch": 0, "rejected_unmapped_url": 0}
        feed_url = ("https://news.google.com/rss/search?q=" + quote_plus(query)
                    + "&hl=en-GB&gl=GB&ceid=GB%3Aen")
        try:
            response = session.get(feed_url, timeout=c.TIMEOUT)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            row["entries_received"] = len(feed.entries)
            report["entries_received"] += len(feed.entries)
            for entry in feed.entries[:25]:
                title = c.textval(entry.get("title"))
                summary = c.textval(entry.get("summary", entry.get("description", "")))
                published = c.parse_date(entry)
                source_meta = entry.get("source", {}) or {}
                source_title = c.textval(source_meta.get("title", ""))
                source_href = source_meta.get("href", "")
                raw_url = entry.get("link", "")
                if len(row["sample_entries"]) < 8:
                    row["sample_entries"].append({
                        "title": title[:180], "published": published.isoformat() if published else None,
                        "source_title": source_title, "source_href": source_href,
                        "link": raw_url[:240]
                    })
                if not title or not published or published < cutoff or published > c.now() + timedelta(hours=12):
                    row["rejected_stale"] += 1
                    continue
                if "chelsea" not in (source_title + " " + source_href).lower():
                    row["rejected_source_mismatch"] += 1
                    continue
                try:
                    resolved = session.get(raw_url, timeout=c.TIMEOUT, allow_redirects=True)
                    final_url = c.normalize_url(resolved.url)
                    if not is_official_article_url(final_url):
                        report["rejected_non_official"] += 1
                        row["rejected_unmapped_url"] += 1
                        continue
                    if final_url in seen:
                        continue
                    seen.add(final_url)
                    item = c.observation(title, summary, final_url,
                                         "Google News official-source discovery",
                                         "Chelsea Football Club", published)
                    if item:
                        item["publisher_type"] = "official_domain"
                        item["discovery_timestamp_source"] = "Google News RSS; discovery only"
                        observations.append(item)
                        row["observations_collected"] += 1
                except requests.RequestException:
                    report["rejected_non_official"] += 1
            row["status"] = "success" if row["observations_collected"] else "empty"
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        report["query_reports"].append(row)
    report["observations_collected"] = len(observations)
    report["status"] = "success" if observations else "empty"
    if not observations:
        report["error"] = "Google News returned no fresh results resolving to direct Chelsea article URLs"
    return observations, report


def discover_bing_news(session):
    """Use Bing News only for discovery; resolve every result to its publisher."""
    from urllib.parse import quote_plus
    queries = ["Chelsea FC", "Chelsea Women", "Chelsea Women Manchester City 1-0",
               "Veerle Buurman Chelsea", "Malo Gusto Chelsea",
               "site:chelseafc.com Wieke Kaptein", "site:chelseafc.com Chelsea news"]
    report = {"source": "Bing News Chelsea discovery", "url": "https://www.bing.com/news/",
              "status": "failed", "entries_received": 0,
              "observations_collected": 0, "rejected_unresolved": 0,
              "query_reports": [], "error": None}
    observations, seen = [], set()
    try:
        for query in queries:
            feed_url = "https://www.bing.com/news/search?q=" + quote_plus(query) + "&format=rss"
            query_report = {"query": query, "entries_received": 0, "observations_collected": 0, "status": "failed"}
            try:
                response = session.get(feed_url, timeout=c.TIMEOUT)
                response.raise_for_status()
                feed = feedparser.parse(response.content)
                query_report["entries_received"] = len(feed.entries)
                report["entries_received"] += len(feed.entries)
                for entry in feed.entries[:12]:
                    title = c.textval(entry.get("title"))
                    summary = c.textval(entry.get("summary", entry.get("description", "")))
                    published = c.parse_date(entry)
                    raw_url = entry.get("link", "")
                    if not published or not title or not c.relevant(title, summary):
                        continue
                    try:
                        resolved = session.get(raw_url, timeout=c.TIMEOUT, allow_redirects=True)
                        final_url = c.normalize_url(resolved.url)
                        publisher = c.publisher_type(final_url)
                        if not final_url or publisher in ("aggregator_unresolved", "unknown_domain") or final_url in seen:
                            report["rejected_unresolved"] += 1
                            continue
                        seen.add(final_url)
                        item = c.observation(title, summary, final_url, "Bing News discovery: " + query,
                                             c.domain(final_url), published)
                        if item:
                            observations.append(item)
                            query_report["observations_collected"] += 1
                    except requests.RequestException:
                        report["rejected_unresolved"] += 1
                query_report["status"] = "success" if query_report["observations_collected"] else "empty"
            except Exception as exc:
                query_report["error"] = type(exc).__name__ + ": " + str(exc)[:150]
                logging.info("Bing News query %s unavailable: %s", query, type(exc).__name__)
            report["query_reports"].append(query_report)
        report["observations_collected"] = len(observations)
        report["status"] = "success" if observations else "empty"
        if not observations:
            report["error"] = "No fresh Chelsea results resolved to direct publisher pages"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
        logging.warning("Bing News discovery: %s", report["error"])
    return observations, report

def discover_specialist_feeds(session):
    observations, report = [], []
    cutoff = c.now() - timedelta(hours=c.MAX_AGE_HOURS)
    for feed_cfg in SPECIALIST_FEEDS:
        row = {"source": feed_cfg["name"], "url": feed_cfg["url"], "status": "failed",
               "entries_received": 0, "observations_collected": 0, "error": None}
        try:
            response = session.get(feed_cfg["url"], timeout=c.TIMEOUT)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            row["entries_received"] = len(feed.entries)
            for entry in feed.entries:
                title = c.textval(entry.get("title"))
                summary = c.textval(entry.get("summary", entry.get("description", "")))
                published = c.parse_date(entry)
                if not published or published < cutoff or published > c.now() + timedelta(hours=12):
                    continue
                item = c.observation(title, summary, entry.get("link"), feed_cfg["name"],
                                     feed_cfg["name"], published)
                if item:
                    observations.append(item)
            row["observations_collected"] = sum(x["source_feed"] == feed_cfg["name"] for x in observations)
            row["status"] = "success" if row["observations_collected"] else "empty"
            if row["status"] == "empty":
                row["error"] = "No fresh, directly linked Chelsea stories in this feed"
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
            logging.warning("%s: %s", feed_cfg["name"], row["error"])
        report.append(row)
    return observations, report

def merge_same_international_event(groups):
    """Merge only clearly matched player-goal reports with the same opponent."""
    output = []
    for group in groups:
        match = None
        for existing in output:
            if (group.get("story_type") == existing.get("story_type") == "general_news"
                and group.get("subject_key") and group.get("subject_key") == existing.get("subject_key")
                and group.get("opponent_key") and group.get("opponent_key") == existing.get("opponent_key")
                and "ecuador" in (group.get("title", "") + " " + group.get("best_summary", "")).lower()
                and "ecuador" in (existing.get("title", "") + " " + existing.get("best_summary", "")).lower()
                and re.search(r"\b(score|scored|scores|goal|on target)\b", (group.get("title", "") + " " + group.get("best_summary", "")).lower())
                and re.search(r"\b(score|scored|scores|goal|on target)\b", (existing.get("title", "") + " " + existing.get("best_summary", "")).lower())):
                match = existing
                break
        if match is None:
            output.append(dict(group))
            continue
        all_obs = match["source_observations"] + group["source_observations"]
        match["source_observations"] = sorted(all_obs, key=lambda x: (x["publisher_domain"], x["published"], x["url"]))
        match["source_urls"] = sorted({x["url"] for x in all_obs})
        match["publisher_domains"] = sorted({x["publisher_domain"] for x in all_obs})
        match["source_feeds"] = sorted({x["source_feed"] for x in all_obs})
        match["publisher_count"] = len(match["publisher_domains"])
        match["observation_count"] = len(all_obs)
        match["corroboration_status"] = "multiple_direct_publishers_observed_independence_not_proven"
        match["best_summary"] = max((match.get("best_summary", ""), group.get("best_summary", "")), key=len)
        rank = {"official_domain": 4, "established_domain": 3, "other_direct_domain": 1}
        if rank.get(group.get("representative_publisher_type"), 0) > rank.get(match.get("representative_publisher_type"), 0):
            for key in ("title", "representative_title", "representative_url", "representative_publisher", "representative_publisher_type"):
                match[key] = group.get(key)
    return output

def main():
    if not CANDIDATES.exists():
        raise SystemExit("news_candidates.json is missing; run collect_news.py first")
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ChelseaFeedsSourceDiscovery/1.0)"})
    history = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else {"events": {}}
    official_obs, official_report = discover_official_pages(session)
    sitemap_obs, sitemap_report = discover_official_sitemaps(session)
    pinned_groups, pinned_report = discover_pinned_official_story(session, history)
    specialist_obs, specialist_report = discover_specialist_feeds(session)
    bing_obs, bing_report = discover_bing_news(session)
    google_official_obs, google_official_report = discover_google_news_official(session)
    new_obs = official_obs + sitemap_obs + specialist_obs + bing_obs + google_official_obs
    groups = c.cluster(new_obs)
    groups.extend(pinned_groups)
    for group in groups:
        c.score(group)
        group["verification_status"] = "not_started"
        group["verification_checklist"] = c.verification_checklist(group)
    groups = merge_same_international_event(groups)
    for group in groups:
        c.score(group)
        group["verification_status"] = "not_started"
        group["verification_checklist"] = c.verification_checklist(group)
    existing = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    stories = existing.get("stories", [])
    combined = merge_same_international_event(stories + groups)
    pending = c.filter_pending_candidates(combined, history)
    order = {"P1_OFFICIAL_SOURCE_REVIEW": 0, "P2_ESTABLISHED_REPORT_REVIEW": 1,
             "P3_TRANSFER_RUMOUR_REQUIRES_LABEL": 2, "P3_SPECIALIST_OR_OTHER_SOURCE_REVIEW": 3,
             "P4_OPINION_LOW_PRIORITY": 4}
    pending.sort(key=lambda g: (order.get(g.get("priority"), 9), -g.get("importance_score", 0),
                               g.get("published", ""), g.get("title", "").lower()))
    selected = c.select_candidates(pending)
    existing.update(stories=selected, source_discovery_version="1.3",
                    direct_source_observations=len(new_obs) + len(pinned_groups),
                    direct_official_observations=len(official_obs) + len(pinned_groups),
                    specialist_feed_observations=len(specialist_obs))
    CANDIDATES.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    history_updated = c.update_history(groups, history)
    c.write(HISTORY, history_updated)
    report = json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else {}
    reports = report.setdefault("source_reports", [])
    reports.extend(official_report + [sitemap_report, pinned_report] + specialist_report + [bing_report, google_official_report])
    report.update(source_discovery_version="1.4",
                  direct_source_observations=len(new_obs) + len(pinned_groups),
                  direct_official_observations=len(official_obs) + len(sitemap_obs) + len(google_official_obs) + len(pinned_groups),
                  specialist_feed_observations=len(specialist_obs),
                  bing_discovery_observations=len(bing_obs),
                  selected_pending_candidates=len(selected),
                  recovered_from_history_candidates=sum(1 for x in selected if x.get("recovered_from_history")))
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Direct source discovery: official_pages=%d official_sitemap=%d pinned_official=%d specialist=%d bing=%d pending=%d" %
          (len(official_obs), len(sitemap_obs), len(pinned_groups), len(specialist_obs), len(bing_obs), len(selected)))
    for row in official_report + [sitemap_report, pinned_report] + specialist_report + [bing_report]:
        print("SOURCE_REPORT " + json.dumps(row, ensure_ascii=False))

if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        run_self_tests()
    else:
        main()
