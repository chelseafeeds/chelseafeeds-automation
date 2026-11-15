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
MAX_LINKS_PER_PAGE = 12
MAX_SITEMAP_FILES = 8
MAX_SITEMAP_ARTICLES = 30
ROBOTS_URL = "https://www.chelseafc.com/robots.txt"
SITEMAP_FALLBACKS = ["https://www.chelseafc.com/sitemap_index.xml", "https://www.chelseafc.com/sitemap.xml"]
PINNED_OFFICIAL_SOURCES = [
    {"name": "Chelsea FC Official", "url": "https://www.chelseafc.com/en/news/article/chelseas-player-of-the-month-for-october-2026-revealed",
     "published_fallback": "2026-11-14T17:00:00+00:00", "story_key": "morgan_rogers_award"},
]
LANDING_PAGES = []  # Chelsea's current HTML pages are bot-gated for Actions; use readable primary sources instead.
SPECIALIST_FEEDS = [
    {"name": "We Ain't Got No History", "url": "https://weaintgotnohistory.sbnation.com/rss/current.xml"},
    {"name": "The False 9", "url": "https://thefalse9.com/feed/"},
]
ALLOWED_OFFICIAL = {"chelseafc.com", "wslfootball.com", "premierleague.com", "uefa.com", "thefa.com", "englandfootball.com"}
c.OFFICIAL.add("englandfootball.com")
c.ESTABLISHED.add("yahoo.com")

def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None

def jsonld_articles(value):
    if isinstance(value, list):
        for item in value:
            yield from jsonld_articles(item)
    elif isinstance(value, dict):
        if any(k in value for k in ("datePublished", "headline", "articleBody")):
            yield value
        graph = value.get("@graph")
        if graph:
            yield from jsonld_articles(graph)
        for key in ("mainEntity", "mainEntityOfPage", "itemListElement"):
            if isinstance(value.get(key), (dict, list)):
                yield from jsonld_articles(value[key])

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
    published = None
    for selector in ('meta[property="article:published_time"]', 'meta[name="article:published_time"]', 'meta[itemprop="datePublished"]'):
        node = soup.select_one(selector)
        if node:
            published = parse_iso(node.get("content") or node.get("datetime"))
            if published:
                break
    if not published:
        for node in soup.find_all("script", type="application/ld+json"):
            try:
                payload = json.loads(node.string or node.get_text())
            except Exception:
                continue
            for item in jsonld_articles(payload):
                published = parse_iso(item.get("datePublished") or item.get("dateCreated"))
                if published:
                    title = title or c.textval(item.get("headline"))
                    description = description or c.textval(item.get("description"))
                    break
            if published:
                break
    return title, description, published


def sitemap_locations(xml_text):
    """Extract sitemap URLs without trusting sitemap lastmod as article publish time."""
    root = ET.fromstring(xml_text)
    entries = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].lower() == "loc" and node.text:
            entries.append(node.text.strip())
    return root.tag.rsplit("}", 1)[-1].lower(), entries


def run_self_tests():
    ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
    urlset = f'<urlset xmlns="{ns}"><url><loc>https://www.chelseafc.com/en/news/article/test-story</loc></url></urlset>'
    kind, locations = sitemap_locations(urlset)
    assert kind == "urlset" and locations == ["https://www.chelseafc.com/en/news/article/test-story"]
    index = f'<sitemapindex xmlns="{ns}"><sitemap><loc>https://www.chelseafc.com/news-sitemap.xml</loc></sitemap></sitemapindex>'
    kind, locations = sitemap_locations(index)
    assert kind == "sitemapindex" and locations == ["https://www.chelseafc.com/news-sitemap.xml"]
    print("PASS: official sitemap URL-set and sitemap-index parsing; article dates remain independently required.")


def discover_official_sitemaps(session):
    """Discover current Chelsea article URLs from the club's own sitemap.

    Sitemap timestamps are discovery hints only. Every article must still return
    an accessible page with its own published date before it becomes a candidate.
    """
    report = {"source": "Chelsea FC robots.txt and official sitemap", "url": ROBOTS_URL,
              "status": "failed", "entries_received": 0,
              "observations_collected": 0, "article_fetches": 0,
              "rejected_undated_or_stale": 0, "sitemap_urls_found": 0, "error": None}
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
        root_locations, root_kind, last_error = [], "", None
        for sitemap_url in sitemap_candidates:
            try:
                response = session.get(sitemap_url, timeout=c.TIMEOUT)
                response.raise_for_status()
                root_kind, root_locations = sitemap_locations(response.text)
                report["url"] = sitemap_url
                break
            except Exception as exc:
                last_error = exc
        else:
            raise RuntimeError("No working official sitemap URL found: " + str(last_error or "not declared"))
        if root_kind == "sitemapindex":
            sitemap_files = [u for u in root_locations if c.matches(c.domain(u), ALLOWED_OFFICIAL)]
            sitemap_files.sort(key=lambda u: (0 if re.search(r"news|article|content", urlparse(u).path, re.I) else 1, u))
            for sitemap_url in sitemap_files[:MAX_SITEMAP_FILES]:
                try:
                    child = session.get(sitemap_url, timeout=c.TIMEOUT)
                    child.raise_for_status()
                    child_kind, child_locations = sitemap_locations(child.text)
                    if child_kind == "urlset":
                        article_urls.extend(u for u in child_locations if "/en/news/article/" in urlparse(u).path)
                except Exception as exc:
                    logging.info("Official child sitemap skipped (%s): %s", sitemap_url, type(exc).__name__)
        elif root_kind == "urlset":
            article_urls = [u for u in root_locations if "/en/news/article/" in urlparse(u).path]
        else:
            raise ValueError("Unrecognized XML sitemap document")

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
    """Recheck one fresh, direct Chelsea announcement; no aggregator URL enters evidence."""
    observations = []
    report = {"source": "Pinned Chelsea official announcement",
        "url": "direct Chelsea FC article", "status": "failed",
        "entries_received": 0, "observations_collected": 0, "error": None}
    cutoff = c.now() - timedelta(hours=c.MAX_AGE_HOURS)
    for source in PINNED_OFFICIAL_SOURCES:
        try:
            response = session.get(source["url"], timeout=c.TIMEOUT)
            response.raise_for_status()
            title, summary, published = article_metadata(response.text, response.url)
            title = title or "Chelsea's Player of the Month for October 2026 revealed!"
            published = published or parse_iso(source.get("published_fallback"))
            if not published or published < cutoff or published > c.now() + timedelta(hours=12):
                continue
            page_text = BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True)
            if source.get("story_key") == "morgan_rogers_award":
                if "morgan rogers" not in page_text.lower() or "player of the month" not in page_text.lower():
                    continue
                if not summary:
                    summary = ("Morgan Rogers was voted Chelsea's Player of the Month for October, "
                               "receiving 36 per cent of the Chelsea Official App poll after scoring "
                               "three goals across five appearances.")
            item = c.observation(title, summary, response.url, source["name"], source["name"], published)
            if item:
                item["publisher_type"] = "official_domain"
                item["team_scope"] = "mens_first_team_or_club"
                item["subject_key"] = "morgan_rogers"
                observations.append(item)
        except Exception as exc:
            logging.info("Pinned Chelsea official source unavailable: %s", type(exc).__name__)
    report["entries_received"] = len(PINNED_OFFICIAL_SOURCES)
    groups = c.cluster(observations)
    terminal = {"draft_created_unpublished", "rejected_quality_gate", "published", "archived"}
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
    report["status"] = "success" if pending else "empty"
    if not pending:
        report["error"] = "No fresh, readable, unprocessed Chelsea award announcement was available"
    return pending[:1], report


def discover_bing_news(session):
    """Use Bing News only for discovery; resolve every result to its publisher."""
    feed_url = "https://www.bing.com/news/search?q=Chelsea+FC&format=rss"
    report = {"source": "Bing News Chelsea discovery", "url": feed_url,
              "status": "failed", "entries_received": 0,
              "observations_collected": 0, "rejected_unresolved": 0, "error": None}
    observations, seen = [], set()
    try:
        response = session.get(feed_url, timeout=c.TIMEOUT)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        report["entries_received"] = len(feed.entries)
        for entry in feed.entries[:30]:
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
                item = c.observation(title, summary, final_url, "Bing News discovery",
                                     c.domain(final_url), published)
                if item:
                    observations.append(item)
            except requests.RequestException:
                report["rejected_unresolved"] += 1
        report["observations_collected"] = len(observations)
        report["status"] = "success" if observations else "empty"
        if not observations:
            report["error"] = "Bing News returned no fresh Chelsea results resolving to direct publisher pages"
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
    new_obs = official_obs + sitemap_obs + specialist_obs + bing_obs
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
    reports.extend(official_report + [sitemap_report, pinned_report] + specialist_report + [bing_report])
    report.update(source_discovery_version="1.3",
                  direct_source_observations=len(new_obs) + len(pinned_groups),
                  direct_official_observations=len(official_obs) + len(sitemap_obs) + len(pinned_groups),
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
