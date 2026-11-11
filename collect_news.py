#!/usr/bin/env python3
"""ChelseaFeeds Collector v3.5 — Quality-Controlled Edition.

Collects Chelsea-related RSS and NewsNow candidates, resolves aggregator URLs
conservatively, deduplicates stories, ranks them, and records only stories
actually queued in news_candidates.json. It does not verify factual claims,
generate articles, or publish to Blogger/X.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup

VERSION = "3.5"
BASE_DIR = Path(__file__).resolve().parent
HISTORY_FILE = BASE_DIR / "seen_stories.json"
CANDIDATES_FILE = BASE_DIR / "news_candidates.json"
REPORT_FILE = BASE_DIR / "collection_report.json"

REQUEST_TIMEOUT = 18
MAX_STORIES = 50
MAX_NEWSNOW_ITEMS = 80
MAX_HISTORY_ITEMS = 10000
HISTORY_DAYS = 60
STORY_MAX_AGE_DAYS = 7
SIMILARITY_THRESHOLD = 0.88

FEEDS = [
    {"name": "BBC Sport", "url": "https://feeds.bbci.co.uk/sport/football/rss.xml"},
    {"name": "Sky Sports", "url": "https://www.skysports.com/rss/12040"},
    {"name": "Google News Chelsea", "url": "https://news.google.com/rss/search?q=Chelsea+FC+when:7d&hl=en-GB&gl=GB&ceid=GB:en"},
    {"name": "The Guardian", "url": "https://www.theguardian.com/football/chelsea/rss"},
    {"name": "TalkChelsea", "url": "https://www.talkchelsea.net/feed/"},
    {"name": "Chelsea.news", "url": "https://chelsea.news/feed"},
    {"name": "Chelsea FC Latest News", "url": "https://chelseafclatestnews.com/feed"},
    {"name": "Football365", "url": "https://www.football365.com/rss"},
    {"name": "Official Chelsea News Search", "url": "https://news.google.com/rss/search?q=site%3Achelseafc.com%2Fen%2Fnews&hl=en-GB&gl=GB&ceid=GB:en"},
]
NEWSNOW_URL = "https://www.newsnow.com/us/?search=Chelsea"

OFFICIAL_DOMAINS = {"chelseafc.com", "premierleague.com", "uefa.com", "thefa.com"}
ESTABLISHED_DOMAINS = {
    "bbc.co.uk", "bbc.com", "skysports.com", "theguardian.com", "reuters.com",
    "apnews.com", "espn.com", "independent.co.uk", "standard.co.uk", "nytimes.com",
    "theathletic.com", "football365.com", "thetimes.com", "telegraph.co.uk",
}
AGGREGATOR_DOMAINS = {"news.google.com", "newsnow.com", "c.newsnow.com"}

CHELSEA_TERMS = (
    "chelsea", "the blues", "stamford bridge", "chelsea fc", "chelsea women",
    "chelsea academy", "chelsea u21", "chelsea u18",
)
IRRELEVANT_TERMS = (
    "chelsea flower show", "chelsea boots", "chelsea handler", "chelsea manning",
    "chelsea hotel", "chelsea neighbourhood", "chelsea neighborhood",
)
TRANSFER_TERMS = (
    "transfer", "sign", "signing", "bid", "deal", "move", "loan", "contract",
    "interest", "target", "agreed", "fee", "departure", "depart",
)
RUMOUR_TERMS = (
    "rumour", "rumor", "reportedly", "linked", "could sign", "may sign", "claim",
    "speculation", "potential", "talks", "according to reports", "understood to be",
    "considering", "monitoring",
)
CONFIRMATION_TERMS = (
    "confirmed", "officially announced", "announced", "completes", "completed",
    "has signed", "signs for", "joins chelsea", "appointed", "extends contract",
)
INJURY_TERMS = (
    "injury", "injured", "fitness", "hamstring", "knee", "ankle", "ruled out",
    "return to training", "sidelined", "surgery", "rehabilitation",
)
TEAM_TERMS = (
    "squad", "starting xi", "line-up", "lineup", "team news", "availability",
    "training", "press conference", "selection", "starting eleven",
)
MATCH_TERMS = (
    "match report", "match reaction", "premier league", "champions league", "fa cup",
    "carabao cup", "kick-off", "fixture", " vs ", " v ", "full-time", "half-time",
    "matchday", "league table",
)
OPINION_TERMS = (
    "opinion", "analysis", "why", "should", "tactical", "explained", "player ratings",
    "power ranking", "verdict", "view",
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(str(x) for x in value)
    if isinstance(value, dict):
        value = value.get("value", "")
    result = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", result).strip()


def domain_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def domain_matches(domain: str, known: set[str]) -> bool:
    return any(domain == item or domain.endswith("." + item) for item in known)


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    query = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
        and k.lower() not in {"oc", "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source"}
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", urlencode(query), ""))


def is_aggregator(url: str) -> bool:
    return domain_matches(domain_of(url), AGGREGATOR_DOMAINS)


def publisher_type(url: str) -> str:
    domain = domain_of(url)
    if domain_matches(domain, OFFICIAL_DOMAINS):
        return "official_domain"
    if domain_matches(domain, ESTABLISHED_DOMAINS):
        return "established_domain"
    if is_aggregator(url):
        return "aggregator_unresolved"
    return "other_direct_domain" if domain else "unknown_domain"


def parse_date(entry) -> datetime | None:
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if value:
            try:
                dt = parsedate_to_datetime(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def is_chelsea_relevant(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    if any(term in text for term in IRRELEVANT_TERMS):
        return False
    return any(term in text for term in CHELSEA_TERMS)


def classify_story(title: str, summary: str) -> str:
    text = f"{title} {summary}".lower()
    has_transfer = any(term in text for term in TRANSFER_TERMS)
    has_rumour = any(term in text for term in RUMOUR_TERMS)
    if has_transfer and has_rumour:
        return "transfer_rumour"
    if any(term in text for term in INJURY_TERMS):
        return "injury_update"
    if any(term in text for term in TEAM_TERMS):
        return "team_news"
    if any(term in text for term in OPINION_TERMS):
        return "opinion_analysis"
    if any(term in text for term in MATCH_TERMS):
        return "match_or_competition"
    if has_transfer:
        return "transfer_update"
    return "general_news"


def resolve_article_url(url: str, session: requests.Session) -> tuple[str, str]:
    """Resolve aggregator URLs only through redirects or explicit canonical metadata."""
    normalized = normalize_url(url)
    if not normalized:
        return "", "invalid_url"
    if not is_aggregator(normalized):
        return normalized, "direct_feed_url"
    try:
        response = session.get(normalized, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        final_url = normalize_url(response.url or normalized)
        if final_url and not is_aggregator(final_url):
            return final_url, "redirect_resolved"
        soup = BeautifulSoup(response.text[:1_000_000], "html.parser")
        candidates = []
        canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
        if canonical and canonical.get("href"):
            candidates.append(canonical.get("href"))
        for attrs in ({"property": "og:url"}, {"name": "twitter:url"}):
            tag = soup.find("meta", attrs)
            if tag and tag.get("content"):
                candidates.append(tag.get("content"))
        for candidate in candidates:
            absolute = normalize_url(urljoin(response.url or normalized, candidate))
            if absolute and not is_aggregator(absolute):
                return absolute, "canonical_resolved"
    except requests.RequestException as exc:
        logging.debug("Aggregator URL resolution failed for %s: %s", url, exc)
    except Exception as exc:
        logging.debug("URL resolution issue for %s: %s", url, exc)
    return normalized, "aggregator_unresolved"


def publisher_hint(entry) -> str:
    """RSS source names are metadata hints only; never use them as verification."""
    source = entry.get("source")
    if isinstance(source, dict):
        name = clean_text(source.get("title", ""))
        if name and name.lower() not in {"google news", "newsnow"}:
            return name[:100]
    return ""


def stable_id(url: str, title_key: str) -> str:
    seed = normalize_url(url) or title_key
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def make_story(title, summary, url, source_name, publisher_name, published, session, method):
    title = clean_text(title)
    summary = clean_text(summary)
    if not title or not url or not is_chelsea_relevant(title, summary):
        return None
    now = utc_now()
    if published and published < now - timedelta(days=STORY_MAX_AGE_DAYS):
        return None
    resolved_url, resolution = resolve_article_url(url, session)
    domain = domain_of(resolved_url)
    ptype = publisher_type(resolved_url)
    if domain and not is_aggregator(resolved_url):
        publisher = domain
    else:
        publisher = publisher_name or "Unresolved publisher"
    story_type = classify_story(title, summary)
    title_key = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    published_iso = published.isoformat() if published else None
    return {
        "id": stable_id(resolved_url, title_key),
        "title_key": title_key,
        "title": title,
        "summary": summary[:1600],
        "url": resolved_url,
        "original_url": normalize_url(url),
        "normalized_url": normalize_url(resolved_url),
        "url_resolution": resolution,
        "url_resolved": resolution != "aggregator_unresolved" and bool(resolved_url),
        "source_feeds": [source_name],
        "publisher": publisher,
        "publisher_name_hint": publisher_name or None,
        "publisher_type": ptype,
        "publisher_domain": domain or None,
        "published": published_iso,
        "date_unverified": published is None,
        "story_type": story_type,
        "verification_status": (
            "official_domain_not_claim_verified" if ptype == "official_domain" else
            "established_domain_not_claim_verified" if ptype == "established_domain" else
            "aggregator_link_unresolved" if ptype == "aggregator_unresolved" else
            "direct_domain_not_claim_verified" if domain else "publisher_unresolved"
        ),
        "direct_publisher_domains": [domain] if domain and not is_aggregator(resolved_url) else [],
        "corroborating_publishers": [],
        "corroboration_count": 0,
        "corroboration_status": "not_independently_corroborated",
        "priority": "P3_DEVELOPING_OR_UNCONFIRMED",
        "importance_score": 0,
        "discovery_methods": [method],
        "queue_status": "candidate_not_yet_queued",
        "previously_queued": False,
    }


def fetch_rss(config, session):
    stories = []
    report = {"source": config["name"], "url": config["url"], "status": "failed",
              "entries_received": 0, "chelsea_relevant": 0, "stories_collected": 0,
              "rejected_old": 0, "rejected_irrelevant": 0, "error": None}
    try:
        response = session.get(config["url"], timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        if not feed.entries:
            report["error"] = "Feed returned no entries"
            return stories, report
        report["entries_received"] = len(feed.entries)
        for entry in feed.entries:
            published = parse_date(entry)
            if published and published < utc_now() - timedelta(days=STORY_MAX_AGE_DAYS):
                report["rejected_old"] += 1
                continue
            title = clean_text(entry.get("title", ""))
            summary = clean_text(entry.get("summary", entry.get("description", "")))
            if not is_chelsea_relevant(title, summary):
                report["rejected_irrelevant"] += 1
                continue
            report["chelsea_relevant"] += 1
            link = entry.get("link", "")
            story = make_story(title, summary, link, config["name"], publisher_hint(entry), published, session, "rss")
            if story:
                stories.append(story)
        report["status"] = "success"
        report["stories_collected"] = len(stories)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        logging.warning("Feed failed: %s — %s", config["name"], exc)
    return stories, report


def extract_newsnow_items(html):
    """Keep only Chelsea headlines linked directly to a publisher domain."""
    soup = BeautifulSoup(html, "html.parser")
    results, seen = [], set()
    for anchor in soup.select("a[href]"):
        title = clean_text(anchor.get_text(" ", strip=True))
        href = normalize_url(urljoin(NEWSNOW_URL, anchor.get("href", "")))
        if not title or len(title) < 20 or not is_chelsea_relevant(title, "") or not href:
            continue
        if is_aggregator(href) or href in seen:
            continue
        domain = domain_of(href)
        if not domain or domain == "newsnow.com":
            continue
        seen.add(href)
        results.append({"title": title, "url": href, "summary": "", "publisher": domain})
        if len(results) >= MAX_NEWSNOW_ITEMS:
            break
    return results


def fetch_newsnow(session):
    stories = []
    report = {"source": "NewsNow", "url": NEWSNOW_URL, "status": "failed",
              "entries_received": 0, "stories_collected": 0, "error": None}
    try:
        response = session.get(NEWSNOW_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        items = extract_newsnow_items(response.text)
        report["entries_received"] = len(items)
        for item in items:
            story = make_story(item["title"], item["summary"], item["url"], "NewsNow",
                               item["publisher"], None, session, "newsnow_direct_link")
            if story:
                stories.append(story)
        report["status"] = "success"
        report["stories_collected"] = len(stories)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        logging.warning("NewsNow collection failed: %s", exc)
    return stories, report


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def deduplicate(stories):
    """Merge same-URL or near-identical headlines; corroboration is domain-based."""
    groups = []
    for story in stories:
        match = None
        for existing in groups:
            same_url = bool(story.get("normalized_url")) and story["normalized_url"] == existing.get("normalized_url")
            similar = title_similarity(story.get("title_key", ""), existing.get("title_key", "")) >= SIMILARITY_THRESHOLD
            if same_url or similar:
                match = existing
                break
        if match is None:
            groups.append(story)
            continue
        match["duplicate_observations"] = match.get("duplicate_observations", 0) + 1
        match["source_feeds"] = sorted(set(match.get("source_feeds", []) + story.get("source_feeds", [])))
        match["discovery_methods"] = sorted(set(match.get("discovery_methods", []) + story.get("discovery_methods", [])))
        domains = set(match.get("direct_publisher_domains", [])) | set(story.get("direct_publisher_domains", []))
        domains.discard("")
        match["direct_publisher_domains"] = sorted(domains)
        if len(domains) >= 2:
            match["corroborating_publishers"] = sorted(domains)
            match["corroboration_count"] = len(domains)
            match["corroboration_status"] = "multiple_direct_domains_detected_review_independence"
        elif len(domains) == 1:
            match["corroborating_publishers"] = sorted(domains)
            match["corroboration_count"] = 1
            match["corroboration_status"] = "one_direct_domain_detected"
        if len(story.get("summary", "")) > len(match.get("summary", "")):
            match["summary"] = story["summary"]
        # Prefer an official/established direct article URL over an unresolved aggregator.
        current_rank = {"official_domain": 3, "established_domain": 2, "other_direct_domain": 1, "aggregator_unresolved": 0, "unknown_domain": 0}
        if current_rank.get(story.get("publisher_type"), 0) > current_rank.get(match.get("publisher_type"), 0):
            for key in ("url", "normalized_url", "url_resolution", "url_resolved", "publisher", "publisher_type", "publisher_domain", "verification_status"):
                match[key] = story.get(key)
            match["id"] = story["id"]
    for story in groups:
        story.setdefault("duplicate_observations", 0)
        domains = story.get("direct_publisher_domains", [])
        if len(domains) >= 2:
            story["corroborating_publishers"] = sorted(domains)
            story["corroboration_count"] = len(domains)
            story["corroboration_status"] = "multiple_direct_domains_detected_review_independence"
        elif len(domains) == 1:
            story["corroborating_publishers"] = sorted(domains)
            story["corroboration_count"] = 1
            story["corroboration_status"] = "one_direct_domain_detected"
        else:
            story["corroboration_count"] = 0
            story["corroboration_status"] = "not_independently_corroborated"
    return groups


def importance_and_priority(story):
    text = f"{story.get('title', '')} {story.get('summary', '')}".lower()
    ptype = story.get("publisher_type")
    stype = story.get("story_type")
    confirmed_language = any(term in text for term in CONFIRMATION_TERMS)
    rumour = stype == "transfer_rumour"
    score = 20
    if ptype == "official_domain":
        score += 35
    elif ptype == "established_domain":
        score += 22
    elif ptype == "other_direct_domain":
        score += 5
    if stype in {"injury_update", "team_news", "transfer_update", "transfer_rumour"}:
        score += 10
    if confirmed_language:
        score += 8
    if story.get("corroboration_count", 0) >= 2:
        score += min(12, story["corroboration_count"] * 3)
    if story.get("date_unverified"):
        score -= 8
    if not story.get("url_resolved"):
        score -= 12
    if rumour:
        score -= 8
    if ptype == "official_domain":
        priority = "P1_OFFICIAL_SOURCE_REVIEW"
    elif ptype == "established_domain" and not rumour:
        priority = "P1_OR_P2_ESTABLISHED_REPORT"
    elif rumour:
        priority = "P3_RUMOUR_MUST_BE_LABELLED"
    else:
        priority = "P3_DEVELOPING_OR_UNCONFIRMED"
    story["importance_score"] = max(0, min(100, score))
    story["priority"] = priority


def load_history():
    if not HISTORY_FILE.exists():
        return {}
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("stories"), dict):
            return data["stories"]
        if isinstance(data, dict):  # legacy v3.2/v3.4 history
            return data
    except Exception as exc:
        logging.warning("Could not read history; starting with empty history: %s", exc)
    return {}


def history_keys(story):
    keys = []
    if story.get("normalized_url"):
        keys.append("url:" + hashlib.sha256(story["normalized_url"].encode()).hexdigest())
    if story.get("title_key"):
        keys.append("title:" + hashlib.sha256(story["title_key"].encode()).hexdigest())
    return keys


def prune_history(history):
    cutoff = utc_now() - timedelta(days=HISTORY_DAYS)
    cleaned = {}
    for key, value in history.items():
        # v3.2-v3.4 wrote every discovered story into history, including
        # stories never selected. Importing those entries would preserve the
        # old suppression bug, so only explicit queued records are retained.
        if not isinstance(value, dict) or not value.get("queued_at"):
            continue
        stamp = value.get("queued_at")
        try:
            dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                cleaned[key] = value if isinstance(value, dict) else {"queued_at": dt.isoformat(), "status": "legacy_queued"}
        except Exception:
            continue
    return cleaned


def mark_selected_queued(selected, history):
    """Only selected queue items enter history; unselected candidates stay eligible."""
    now = utc_now().isoformat()
    updated = dict(history)
    for story in selected:
        story["queue_status"] = "queued_for_next_stage"
        story["queued_at"] = now
        for key in history_keys(story):
            updated[key] = {"queued_at": now, "status": "queued", "story_id": story["id"], "title": story["title"][:180]}
    if len(updated) > MAX_HISTORY_ITEMS:
        ordered = sorted(updated.items(), key=lambda item: (item[1].get("queued_at", "") if isinstance(item[1], dict) else str(item[1])))
        updated = dict(ordered[-MAX_HISTORY_ITEMS:])
    return updated


def write_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    started = utc_now()
    cutoff = started - timedelta(days=STORY_MAX_AGE_DAYS)
    session = requests.Session()
    session.headers.update({"User-Agent": f"Mozilla/5.0 (compatible; ChelseaFeedsBot/{VERSION}; +https://www.chelseafeeds.com/)"})
    all_stories, source_reports = [], []

    for config in FEEDS:
        logging.info("Collecting: %s", config["name"])
        stories, source_report = fetch_rss(config, session)
        all_stories.extend(stories)
        source_reports.append(source_report)
        time.sleep(0.2)

    logging.info("Collecting: NewsNow")
    stories, source_report = fetch_newsnow(session)
    all_stories.extend(stories)
    source_reports.append(source_report)

    unique_stories = deduplicate(all_stories)
    history = prune_history(load_history())
    eligible, already_queued = [], 0
    for story in unique_stories:
        keys = history_keys(story)
        was_queued = any(key in history for key in keys)
        story["previously_queued"] = was_queued
        if was_queued:
            already_queued += 1
        else:
            eligible.append(story)

    for story in eligible:
        importance_and_priority(story)

    priority_order = {"P1_OFFICIAL_SOURCE_REVIEW": 0, "P1_OR_P2_ESTABLISHED_REPORT": 1,
                      "P3_RUMOUR_MUST_BE_LABELLED": 2, "P3_DEVELOPING_OR_UNCONFIRMED": 3}
    def published_timestamp(item):
        try:
            value = datetime.fromisoformat(item.get("published", "").replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.timestamp()
        except Exception:
            return 0

    eligible.sort(key=lambda item: (priority_order.get(item["priority"], 9),
                                    -item["importance_score"], -published_timestamp(item),
                                    item.get("title", "").lower()))
    selected = eligible[:MAX_STORIES]
    updated_history = mark_selected_queued(selected, history)

    for story in selected:
        story["corroboration_note"] = (
            "Distinct direct publisher domains were detected; editorial review is still required to establish independent reporting."
            if story.get("corroboration_count", 0) >= 2 else
            "No independent corroboration has been established by this collector."
        )

    completed = utc_now()
    source_counts = Counter(item.get("status", "unknown") for item in source_reports)
    priority_counts = Counter(item.get("priority", "unknown") for item in selected)
    report_data = {
        "collector_version": VERSION,
        "run_started_at": started.isoformat(),
        "run_completed_at": completed.isoformat(),
        "duration_seconds": round((completed - started).total_seconds(), 2),
        "configured_sources": len(source_reports),
        "successful_sources": source_counts.get("success", 0),
        "failed_sources": source_counts.get("failed", 0),
        "total_collected_before_deduplication": len(all_stories),
        "unique_stories": len(unique_stories),
        "duplicate_observations_merged": max(0, len(all_stories) - len(unique_stories)),
        "already_queued_stories_suppressed": already_queued,
        "eligible_not_previously_queued": len(eligible),
        "selected_and_queued": len(selected),
        "eligible_but_not_selected": max(0, len(eligible) - len(selected)),
        "selected_priority_distribution": dict(priority_counts),
        "selected_unresolved_aggregator_links": sum(1 for s in selected if not s.get("url_resolved")),
        "selected_direct_links": sum(1 for s in selected if s.get("url_resolved")),
        "selected_date_unverified": sum(1 for s in selected if s.get("date_unverified")),
        "source_reports": source_reports,
        "quality_notes": [
            "A candidate is not a verified fact. Editors or a later verification stage must check claims against source articles.",
            "Official-domain status identifies the website domain, not the truth of every statement in the article.",
            "RSS publisher names and headline suffixes are metadata only and do not establish verification.",
            "Corroboration counts distinct direct publisher domains only; even this does not prove editorial independence.",
            "Unresolved Google News or NewsNow links are retained as unresolved and must not be cited as direct publisher articles.",
            "Only selected stories are recorded as queued. Unselected candidates remain eligible on later runs while they remain in source feeds and within the age window.",
            "This collector does not generate articles or publish to Blogger or X.",
        ],
    }
    candidates_data = {"collector_version": VERSION, "generated_at": completed.isoformat(),
                       "selection_limit": MAX_STORIES, "stories": selected}
    history_data = {"history_version": VERSION, "updated_at": completed.isoformat(), "stories": updated_history}

    write_json(CANDIDATES_FILE, candidates_data)
    write_json(REPORT_FILE, report_data)
    write_json(HISTORY_FILE, history_data)
    logging.info("Completed v%s: %d collected, %d unique, %d eligible, %d queued; %d already queued.",
                 VERSION, len(all_stories), len(unique_stories), len(eligible), len(selected), already_queued)
    logging.info("Saved %s, %s and %s", CANDIDATES_FILE.name, REPORT_FILE.name, HISTORY_FILE.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
