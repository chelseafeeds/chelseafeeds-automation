#!/usr/bin/env python3
"""ChelseaFeeds Collector v3.6 — Publisher-Verified Queue Edition.

Collects direct publisher RSS feeds, resolves aggregator URLs conservatively,
rejects unresolved/undated links from the editorial queue, deduplicates, ranks,
and records only stories actually queued. It does not verify claims, write
articles, or publish to Blogger/X.
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

VERSION = "3.6"
BASE_DIR = Path(__file__).resolve().parent
HISTORY_FILE = BASE_DIR / "seen_stories.json"
CANDIDATES_FILE = BASE_DIR / "news_candidates.json"
REPORT_FILE = BASE_DIR / "collection_report.json"

REQUEST_TIMEOUT = 18
MAX_STORIES = 40
MAX_HISTORY_ITEMS = 10000
HISTORY_DAYS = 60
STORY_MAX_AGE_DAYS = 7
SIMILARITY_THRESHOLD = 0.88

# Direct publisher feeds only. Google News and NewsNow are excluded because
# their current links frequently remain aggregator URLs or produce noisy leads.
FEEDS = [
    {"name": "BBC Sport", "url": "https://feeds.bbci.co.uk/sport/football/rss.xml"},
    {"name": "Sky Sports", "url": "https://www.skysports.com/rss/12040"},
    {"name": "The Guardian Chelsea", "url": "https://www.theguardian.com/football/chelsea/rss"},
    {"name": "TalkChelsea", "url": "https://www.talkchelsea.net/feed/"},
    {"name": "Chelsea.news", "url": "https://chelsea.news/feed"},
    {"name": "Chelsea FC Latest News", "url": "https://chelseafclatestnews.com/feed"},
    {"name": "Football365", "url": "https://www.football365.com/rss"},
]

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


def utc_now():
    return datetime.now(timezone.utc)


def clean_text(value):
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(str(x) for x in value)
    if isinstance(value, dict):
        value = value.get("value", "")
    return re.sub(r"\s+", " ", BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)).strip()


def domain_of(url):
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def domain_matches(domain, known):
    return any(domain == item or domain.endswith("." + item) for item in known)


def normalize_url(url):
    if not url:
        return ""
    parsed = urlparse(str(url).strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    query = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
        and k.lower() not in {"oc", "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source"}
    ]
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(),
                       parsed.path.rstrip("/") or "/", "", urlencode(query), ""))


def is_aggregator(url):
    return domain_matches(domain_of(url), AGGREGATOR_DOMAINS)


def publisher_type(url):
    domain = domain_of(url)
    if domain_matches(domain, OFFICIAL_DOMAINS):
        return "official_domain"
    if domain_matches(domain, ESTABLISHED_DOMAINS):
        return "established_domain"
    if is_aggregator(url):
        return "aggregator_unresolved"
    return "other_direct_domain" if domain else "unknown_domain"


def parse_date(entry):
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


def is_chelsea_relevant(title, summary):
    text = f"{title} {summary}".lower()
    return not any(term in text for term in IRRELEVANT_TERMS) and any(term in text for term in CHELSEA_TERMS)


def classify_story(title, summary):
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


def resolve_article_url(url, session):
    """Resolve only by HTTP redirect or explicit canonical metadata; never guess."""
    normalized = normalize_url(url)
    if not normalized:
        return "", "invalid_url"
    if not is_aggregator(normalized):
        return normalized, "direct_feed_url"
    try:
        response = session.get(normalized, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        response.raise_for_status()
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
    except Exception as exc:
        logging.debug("Could not resolve %s: %s", url, exc)
    return normalized, "aggregator_unresolved"


def publisher_hint(entry):
    source = entry.get("source")
    if isinstance(source, dict):
        name = clean_text(source.get("title", ""))
        if name and name.lower() not in {"google news", "newsnow"}:
            return name[:100]
    return ""


def stable_id(url, title_key):
    return hashlib.sha256((normalize_url(url) or title_key).encode("utf-8")).hexdigest()[:20]


def make_story(title, summary, url, source_name, publisher_name, published, session):
    title, summary = clean_text(title), clean_text(summary)
    if not title or not url or not is_chelsea_relevant(title, summary):
        return None
    if published is None:
        return None
    if published > utc_now() + timedelta(hours=12) or published < utc_now() - timedelta(days=STORY_MAX_AGE_DAYS):
        return None
    resolved_url, resolution = resolve_article_url(url, session)
    domain = domain_of(resolved_url)
    ptype = publisher_type(resolved_url)
    if not domain or is_aggregator(resolved_url):
        return None
    story_type = classify_story(title, summary)
    title_key = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    published_iso = published.isoformat()
    return {
        "id": stable_id(resolved_url, title_key),
        "title_key": title_key,
        "title": title,
        "summary": summary[:1600],
        "url": resolved_url,
        "original_url": normalize_url(url),
        "normalized_url": normalize_url(resolved_url),
        "url_resolution": resolution,
        "url_resolved": True,
        "source_feeds": [source_name],
        "publisher": domain,
        "publisher_name_hint": publisher_name or None,
        "publisher_type": ptype,
        "publisher_domain": domain,
        "published": published_iso,
        "date_unverified": False,
        "story_type": story_type,
        "verification_status": (
            "official_domain_requires_claim_check" if ptype == "official_domain" else
            "established_publisher_requires_claim_check" if ptype == "established_domain" else
            "direct_publisher_requires_editorial_check"
        ),
        "direct_publisher_domains": [domain],
        "corroborating_publishers": [],
        "corroboration_count": 1,
        "corroboration_status": "one_direct_domain_observed_not_independent_confirmation",
        "priority": "P3_DEVELOPING_OR_UNCONFIRMED",
        "importance_score": 0,
        "queue_status": "candidate_not_yet_queued",
        "previously_queued": False,
    }


def fetch_rss(config, session):
    stories = []
    report = {"source": config["name"], "url": config["url"], "status": "failed",
              "entries_received": 0, "chelsea_relevant": 0, "stories_collected": 0,
              "rejected_old": 0, "rejected_undated": 0, "rejected_irrelevant": 0,
              "rejected_unresolved_aggregator": 0, "error": None}
    try:
        response = session.get(config["url"], timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        if not feed.entries:
            report["status"] = "empty"
            report["error"] = "Feed returned zero entries"
            return stories, report
        report["entries_received"] = len(feed.entries)
        for entry in feed.entries:
            title = clean_text(entry.get("title", ""))
            summary = clean_text(entry.get("summary", entry.get("description", "")))
            if not is_chelsea_relevant(title, summary):
                report["rejected_irrelevant"] += 1
                continue
            report["chelsea_relevant"] += 1
            published = parse_date(entry)
            if published is None:
                report["rejected_undated"] += 1
                continue
            if published > utc_now() + timedelta(hours=12) or published < utc_now() - timedelta(days=STORY_MAX_AGE_DAYS):
                report["rejected_old"] += 1
                continue
            link = entry.get("link", "")
            before = len(stories)
            story = make_story(title, summary, link, config["name"], publisher_hint(entry), published, session)
            if story:
                stories.append(story)
            elif is_aggregator(link):
                report["rejected_unresolved_aggregator"] += 1
        report["stories_collected"] = len(stories)
        report["status"] = "success" if stories else "empty"
        if not stories:
            report["error"] = "No publish-queue-eligible direct Chelsea stories in this feed"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        logging.warning("Feed failed: %s — %s", config["name"], exc)
    return stories, report


def title_similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def deduplicate(stories):
    groups = []
    for story in stories:
        match = None
        for existing in groups:
            if (story.get("normalized_url") and story["normalized_url"] == existing.get("normalized_url")) or (
                title_similarity(story.get("title_key", ""), existing.get("title_key", "")) >= SIMILARITY_THRESHOLD
            ):
                match = existing
                break
        if match is None:
            groups.append(story)
            continue
        match["duplicate_observations"] = match.get("duplicate_observations", 0) + 1
        match["source_feeds"] = sorted(set(match.get("source_feeds", []) + story.get("source_feeds", [])))
        domains = sorted(set(match.get("direct_publisher_domains", []) + story.get("direct_publisher_domains", [])))
        match["direct_publisher_domains"] = domains
        match["corroborating_publishers"] = domains
        match["corroboration_count"] = len(domains)
        match["corroboration_status"] = (
            "multiple_domains_detected_independence_not_proven" if len(domains) > 1
            else "one_direct_domain_observed_not_independent_confirmation"
        )
        if len(story.get("summary", "")) > len(match.get("summary", "")):
            match["summary"] = story["summary"]
        rank = {"official_domain": 3, "established_domain": 2, "other_direct_domain": 1}
        if rank.get(story["publisher_type"], 0) > rank.get(match["publisher_type"], 0):
            for key in ("url", "normalized_url", "url_resolution", "publisher", "publisher_type",
                        "publisher_domain", "verification_status", "id"):
                match[key] = story[key]
    for story in groups:
        story.setdefault("duplicate_observations", 0)
    return groups


def importance_and_priority(story):
    text = f"{story['title']} {story['summary']}".lower()
    ptype, stype = story["publisher_type"], story["story_type"]
    rumour = stype == "transfer_rumour" or any(term in text for term in RUMOUR_TERMS)
    confirmed_language = any(term in text for term in CONFIRMATION_TERMS)
    score = 25
    if ptype == "official_domain":
        score += 40
    elif ptype == "established_domain":
        score += 25
    else:
        score += 5
    if stype in {"injury_update", "team_news", "transfer_update", "transfer_rumour"}:
        score += 10
    if confirmed_language and ptype == "official_domain":
        score += 8
    if story.get("corroboration_count", 0) >= 2:
        score += 5
    if rumour:
        score -= 12
    if ptype == "official_domain":
        priority = "P1_OFFICIAL_SOURCE_REVIEW"
    elif ptype == "established_domain" and not rumour:
        priority = "P2_ESTABLISHED_REPORT_REVIEW"
    elif rumour:
        priority = "P3_RUMOUR_MUST_BE_LABELLED"
    else:
        priority = "P3_SPECIALIST_OR_OTHER_SOURCE_REVIEW"
    story["importance_score"] = max(0, min(100, score))
    story["priority"] = priority
    story["rumour_language_detected"] = rumour


def load_history():
    if not HISTORY_FILE.exists():
        return {}
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("stories"), dict):
            return data["stories"]
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logging.warning("Could not read history: %s", exc)
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
        if not isinstance(value, dict) or not value.get("queued_at"):
            continue
        try:
            dt = datetime.fromisoformat(str(value["queued_at"]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                cleaned[key] = value
        except Exception:
            continue
    return cleaned


def mark_selected_queued(selected, history):
    now = utc_now().isoformat()
    updated = dict(history)
    for story in selected:
        story["queue_status"] = "queued_for_editorial_verification"
        story["queued_at"] = now
        for key in history_keys(story):
            updated[key] = {"queued_at": now, "status": "queued",
                            "story_id": story["id"], "title": story["title"][:180]}
    if len(updated) > MAX_HISTORY_ITEMS:
        ordered = sorted(updated.items(), key=lambda item: item[1].get("queued_at", ""))
        updated = dict(ordered[-MAX_HISTORY_ITEMS:])
    return updated


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    started = utc_now()
    session = requests.Session()
    session.headers.update({"User-Agent": f"Mozilla/5.0 (compatible; ChelseaFeedsBot/{VERSION}; +https://www.chelseafeeds.com/)"})
    all_stories, source_reports = [], []
    for config in FEEDS:
        logging.info("Collecting direct feed: %s", config["name"])
        stories, report = fetch_rss(config, session)
        all_stories.extend(stories)
        source_reports.append(report)
        time.sleep(0.2)

    unique_stories = deduplicate(all_stories)
    history = prune_history(load_history())
    eligible, already_queued = [], 0
    for story in unique_stories:
        was_queued = any(key in history for key in history_keys(story))
        story["previously_queued"] = was_queued
        if was_queued:
            already_queued += 1
        else:
            eligible.append(story)

    for story in eligible:
        importance_and_priority(story)

    priority_order = {
        "P1_OFFICIAL_SOURCE_REVIEW": 0,
        "P2_ESTABLISHED_REPORT_REVIEW": 1,
        "P3_RUMOUR_MUST_BE_LABELLED": 2,
        "P3_SPECIALIST_OR_OTHER_SOURCE_REVIEW": 3,
    }
    def timestamp(item):
        try:
            return datetime.fromisoformat(item["published"].replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0

    eligible.sort(key=lambda item: (priority_order.get(item["priority"], 9),
                                    -item["importance_score"], -timestamp(item),
                                    item["title"].lower()))
    selected = eligible[:MAX_STORIES]
    updated_history = mark_selected_queued(selected, history)
    completed = utc_now()
    status_counts = Counter(item["status"] for item in source_reports)
    priority_counts = Counter(item["priority"] for item in selected)
    report_data = {
        "collector_version": VERSION,
        "run_started_at": started.isoformat(),
        "run_completed_at": completed.isoformat(),
        "duration_seconds": round((completed - started).total_seconds(), 2),
        "configured_sources": len(source_reports),
        "successful_sources": status_counts.get("success", 0),
        "empty_sources": status_counts.get("empty", 0),
        "failed_sources": status_counts.get("failed", 0),
        "total_collected_before_deduplication": len(all_stories),
        "unique_direct_stories": len(unique_stories),
        "duplicate_observations_merged": max(0, len(all_stories) - len(unique_stories)),
        "already_queued_stories_suppressed": already_queued,
        "eligible_not_previously_queued": len(eligible),
        "selected_and_queued": len(selected),
        "eligible_but_not_selected": max(0, len(eligible) - len(selected)),
        "selected_priority_distribution": dict(priority_counts),
        "selected_unresolved_aggregator_links": sum(1 for s in selected if is_aggregator(s["url"])),
        "selected_direct_publisher_links": sum(1 for s in selected if s.get("url_resolved")),
        "selected_date_unverified": sum(1 for s in selected if s.get("date_unverified")),
        "source_reports": source_reports,
        "excluded_sources": [
            {"source": "Google News Chelsea", "status": "disabled",
             "reason": "Aggregator links were unresolved in v3.5; excluded until reliable direct publisher resolution is available."},
            {"source": "Official Chelsea News Search (Google News)", "status": "disabled",
             "reason": "Search aggregator is not treated as an official Chelsea source."},
            {"source": "NewsNow", "status": "disabled",
             "reason": "HTML extraction returned zero usable items and falsely reported success in prior runs."},
        ],
        "quality_notes": [
            "Queue contains only direct publisher URLs; unresolved aggregator URLs are rejected before selection.",
            "Stories without a trustworthy publication date or outside the seven-day window are rejected.",
            "A direct link or trusted publisher domain does not prove every claim; every queued story still requires factual checks.",
            "Official-domain and established-publisher tiers identify source domains, not factual certainty.",
            "Rumour language is flagged and must remain clearly labelled in any later article.",
            "Specialist blogs and other direct domains are lower priority and require stronger editorial scrutiny.",
            "Only selected stories are recorded as queued; rejected and unselected stories are not permanently consumed.",
            "This collector does not generate articles or publish to Blogger or X.",
        ],
    }
    candidates_data = {"collector_version": VERSION, "generated_at": completed.isoformat(),
                       "selection_limit": MAX_STORIES, "stories": selected}
    history_data = {"history_version": VERSION, "updated_at": completed.isoformat(), "stories": updated_history}
    write_json(CANDIDATES_FILE, candidates_data)
    write_json(REPORT_FILE, report_data)
    write_json(HISTORY_FILE, history_data)
    logging.info("v%s complete: %d direct stories, %d unique, %d eligible, %d queued; %d suppressed.",
                 VERSION, len(all_stories), len(unique_stories), len(eligible), len(selected), already_queued)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
