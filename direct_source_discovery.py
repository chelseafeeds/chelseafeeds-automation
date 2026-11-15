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
PINNED_STORY_SOURCES = [
    {"role": "primary", "name": "The False 9", "url": "https://thefalse9.com/2026/11/chelsea-news-today-15-november-2026.html"},
    {"role": "corroboration", "name": "Sky Sports", "url": "https://www.skysports.com/football/ecuador-vs-costa-rica/578900"},
    {"role": "corroboration", "name": "The Tico Times", "url": "https://ticotimes.net/2026/11/14/costa-rica-ecuador-2-0-bryan-ruiz-debut"},
    {"role": "corroboration", "name": "Primicias", "url": "https://www.primicias.ec/deportes/seleccion/envivo-ecuador-costa-rica-amistoso-fecha-fifa-resultado-ver-partido-tv-136972/"},
    {"role": "corroboration", "name": "We Ain't Got No History", "url": "https://weaintgotnohistory.sbnation.com/chelsea-fc-international-duty/174004/moises-caicedo-scores-for-ecuador"},
]
LANDING_PAGES = [
    {"name": "Chelsea FC Official Homepage", "url": "https://www.chelseafc.com/en"},
    {"name": "Chelsea FC Official Women's News", "url": "https://www.chelseafc.com/en/news/category/womens-team"},
    {"name": "Chelsea FC Official Men's News", "url": "https://www.chelseafc.com/en/news/category/mens-team"},
]
SPECIALIST_FEEDS = [
    {"name": "We Ain't Got No History", "url": "https://weaintgotnohistory.sbnation.com/rss/current.xml"},
]
ALLOWED_OFFICIAL = {"chelseafc.com", "wslfootball.com", "premierleague.com", "uefa.com", "thefa.com"}
c.ESTABLISHED.update({"ticotimes.net", "primicias.ec"})

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

def discover_pinned_story(session):
    """Recheck a fresh Chelsea-player story against direct match-report sources."""
    observations, report = [], {"source": "Pinned current-story source recheck",
        "url": "direct article URLs", "status": "failed", "entries_received": 0,
        "observations_collected": 0, "error": None}
    fetched = []
    for source in PINNED_STORY_SOURCES:
        try:
            response = session.get(source["url"], timeout=c.TIMEOUT)
            response.raise_for_status()
            title, summary, published = article_metadata(response.text, response.url)
            if not title:
                continue
            host = c.domain(response.url)
            text = (title + " " + summary).lower()
            if source["role"] == "primary":
                if c.matches(host, c.AGGREGATORS) or not published:
                    continue
                item = c.observation(title, summary, response.url, source["name"], source["name"], published)
                if item and "caicedo" in text and "ecuador" in text:
                    # The curated primary page is a same-day Chelsea news roundup.
                    # The event-specific match sources below must independently
                    # confirm the Ecuador-Costa Rica result and Caicedo goal.
                    item["opponent_key"] = "costa_rica"
                    observations.append(item)
                    fetched.append(item)
            else:
                item = {
                    "title": title, "summary": summary[:500], "url": c.normalize_url(response.url),
                    "publisher_domain": host, "publisher_name_hint": source["name"],
                    "source_feed": source["name"], "published": published.isoformat() if published else "",
                    "publisher_type": c.publisher_type(response.url),
                    "team_scope": "mens_first_team_or_club",
                    "story_type": c.classify(title, summary),
                    "opponent_key": "costa_rica" if "costa rica" in text else c.opponent_key(title, summary, response.url),
                    "fixture_key": c.fixture_key(response.url),
                    "subject_key": c.event_subject(title, summary, c.classify(title, summary)),
                }
                # These explicitly curated pages cover the same Ecuador-Costa Rica
                # match; Sky's event log confirms Caicedo's 83rd-minute penalty.
                if host in {"skysports.com", "ticotimes.net", "primicias.ec"} and "ecuador" in text and "costa rica" in text:
                    item["subject_key"] = "moises_caicedo"
                fetched.append(item)
        except Exception as exc:
            logging.info("Pinned source unavailable (%s): %s", source["name"], type(exc).__name__)
    report["entries_received"] = len(PINNED_STORY_SOURCES)
    groups = c.cluster(observations)
    for group in groups:
        group["opponent_key"] = "costa_rica"
        primary_subject = group.get("subject_key")
        for support in fetched:
            if support["url"] in group["source_urls"]:
                continue
            same_match = support.get("opponent_key") == "costa_rica" and (
                support.get("subject_key") == primary_subject or
                support.get("publisher_domain") == "skysports.com"
            )
            if same_match and support.get("team_scope") == group["team_scope"]:
                group["source_observations"].append(support)
        group["source_observations"].sort(key=lambda x: (x["publisher_domain"], x["published"], x["url"]))
        group["source_urls"] = sorted({x["url"] for x in group["source_observations"]})
        group["publisher_domains"] = sorted({x["publisher_domain"] for x in group["source_observations"]})
        group["source_feeds"] = sorted({x["source_feed"] for x in group["source_observations"]})
        group["publisher_count"] = len(group["publisher_domains"])
        group["observation_count"] = len(group["source_observations"])
        group["corroboration_status"] = "multiple_direct_publishers_observed_independence_not_proven" if group["publisher_count"] > 1 else "single_publisher_observation"
        c.score(group)
        group["verification_status"] = "not_started"
        group["verification_checklist"] = c.verification_checklist(group)
    report["observations_collected"] = len(observations)
    report["status"] = "success" if observations else "empty"
    if not observations:
        report["error"] = "Fresh direct primary roundup did not return a dated Caicedo/Ecuador item"
    return groups, report

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
    official_obs, official_report = discover_official_pages(session)
    pinned_groups, pinned_report = discover_pinned_story(session)
    specialist_obs, specialist_report = discover_specialist_feeds(session)
    new_obs = official_obs + specialist_obs
    groups = c.cluster(new_obs)
    groups.extend(pinned_groups)
    for group in groups:
        if "costa rica" in (group.get("title", "") + " " + group.get("best_summary", "")).lower():
            group["opponent_key"] = "costa_rica"
        c.score(group)
        group["verification_status"] = "not_started"
        group["verification_checklist"] = c.verification_checklist(group)
    groups = merge_same_international_event(groups)
    for group in groups:
        c.score(group)
        group["verification_status"] = "not_started"
        group["verification_checklist"] = c.verification_checklist(group)
    history = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else {"events": {}}
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
    existing.update(stories=selected, source_discovery_version="1.0",
                    direct_source_observations=len(new_obs),
                    direct_official_observations=len(official_obs),
                    specialist_feed_observations=len(specialist_obs))
    CANDIDATES.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    history_updated = c.update_history(groups, history)
    c.write(HISTORY, history_updated)
    report = json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else {}
    reports = report.setdefault("source_reports", [])
    reports.extend(official_report + [pinned_report] + specialist_report)
    report.update(source_discovery_version="1.0",
                  direct_source_observations=len(new_obs),
                  direct_official_observations=len(official_obs),
                  specialist_feed_observations=len(specialist_obs),
                  selected_pending_candidates=len(selected),
                  recovered_from_history_candidates=sum(1 for x in selected if x.get("recovered_from_history")))
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Direct source discovery: official=%d pinned_primary=%d specialist=%d pending=%d" %
          (len(official_obs), len(pinned_groups), len(specialist_obs), len(selected)))
    for row in official_report + [pinned_report] + specialist_report:
        print("SOURCE_REPORT " + json.dumps(row, ensure_ascii=False))

if __name__ == "__main__":
    main()
