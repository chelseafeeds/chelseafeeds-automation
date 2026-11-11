
#!/usr/bin/env python3
"""ChelseaFeeds News Collector v3.4

Collects Chelsea FC news candidates from RSS feeds and NewsNow.
It does not generate articles, independently verify every claim,
or publish posts to Blogger or X.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)

import feedparser
import requests
from bs4 import BeautifulSoup


# ============================================================
# CONFIGURATION
# ============================================================

VERSION = "3.4"

BASE_DIR = Path(__file__).resolve().parent

HISTORY_FILE = BASE_DIR / "seen_stories.json"
CANDIDATES_FILE = BASE_DIR / "news_candidates.json"
REPORT_FILE = BASE_DIR / "collection_report.json"

REQUEST_TIMEOUT = 25
MAX_STORIES = 50
MAX_NEWSNOW_ITEMS = 100

HISTORY_DAYS = 60
MAX_HISTORY_ITEMS = 5000

CUTOFF = datetime.now(timezone.utc) - timedelta(days=7)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(compatible; ChelseaFeedsBot/3.4; "
        "+https://www.chelseafeeds.com/)"
    )
}


# ============================================================
# NEWS SOURCES
# ============================================================

FEEDS = [

    {
        "name": "BBC Sport",
        "url": "https://feeds.bbci.co.uk/sport/football/rss.xml",
    },

    {
        "name": "Sky Sports",
        "url": "https://www.skysports.com/rss/12040",
    },

    {
        "name": "Google News Chelsea",
        "url": (
            "https://news.google.com/rss/search?"
            "q=Chelsea+FC+when:7d"
            "&hl=en-GB&gl=GB&ceid=GB:en"
        ),
    },

    {
        "name": "The Guardian",
        "url": "https://www.theguardian.com/football/chelsea/rss",
    },

    {
        "name": "TalkChelsea",
        "url": "https://www.talkchelsea.net/feed/",
    },

    {
        "name": "Chelsea.news",
        "url": "https://chelsea.news/feed",
    },

    {
        "name": "Chelsea FC Latest News",
        "url": "https://chelseafclatestnews.com/feed",
    },

    {
        "name": "Football365",
        "url": "https://www.football365.com/rss",
    },

    {
        "name": "Official Chelsea News Search",
        "url": (
            "https://news.google.com/rss/search?"
            "q=site%3Achelseafc.com%2Fen%2Fnews"
            "&hl=en-GB&gl=GB&ceid=GB:en"
        ),
    },

]

NEWSNOW_URL = "https://www.newsnow.com/us/?search=Chelsea"


# ============================================================
# DOMAIN CLASSIFICATION
# ============================================================

OFFICIAL_DOMAINS = {
    "chelseafc.com",
    "premierleague.com",
    "uefa.com",
    "thefa.com",
}

ESTABLISHED_DOMAINS = {
    "bbc.co.uk",
    "bbc.com",
    "skysports.com",
    "theguardian.com",
    "reuters.com",
    "apnews.com",
    "espn.com",
    "independent.co.uk",
    "standard.co.uk",
    "nytimes.com",
    "theathletic.com",
    "football365.com",
    "thetimes.com",
}

AGGREGATOR_DOMAINS = {
    "news.google.com",
    "newsnow.com",
    "c.newsnow.com",
}


# ============================================================
# CONTENT FILTERS
# ============================================================

CHELSEA_TERMS = (
    "chelsea",
    "the blues",
    "stamford bridge",
    "chelsea fc",
    "chelsea women",
    "chelsea academy",
    "chelsea u21",
    "chelsea u18",
)

IRRELEVANT_TERMS = (
    "chelsea flower show",
    "chelsea boots",
    "chelsea handler",
    "chelsea manning",
    "chelsea hotel",
    "chelsea neighbourhood",
    "chelsea neighborhood",
)

TRANSFER_TERMS = (
    "transfer",
    "sign",
    "signing",
    "bid",
    "deal",
    "move",
    "loan",
    "contract",
    "interest",
    "target",
    "agreed",
    "fee",
)

RUMOUR_TERMS = (
    "rumour",
    "rumor",
    "reportedly",
    "linked",
    "could sign",
    "may sign",
    "claim",
    "speculation",
    "potential",
    "talks",
)

INJURY_TERMS = (
    "injury",
    "injured",
    "fitness",
    "hamstring",
    "knee",
    "ankle",
    "ruled out",
    "return to training",
    "sidelined",
)

TEAM_TERMS = (
    "squad",
    "starting xi",
    "line-up",
    "lineup",
    "team news",
    "availability",
    "training",
    "press conference",
)

MATCH_TERMS = (
    "match report",
    "match reaction",
    "premier league",
    "champions league",
    "fa cup",
    "carabao cup",
    "kick-off",
    "fixture",
    "vs ",
    " v ",
)

OPINION_TERMS = (
    "opinion",
    "analysis",
    "why",
    "should",
    "tactical",
    "explained",
    "player ratings",
    "power ranking",
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)


# ============================================================
# TEXT AND URL UTILITIES
# ============================================================

def clean_text(value):

    if value is None:
        return ""

    if isinstance(value, list):
        value = " ".join(str(x) for x in value)

    if isinstance(value, dict):
        value = value.get("value", "")

    text = BeautifulSoup(
        str(value),
        "html.parser"
    ).get_text(" ", strip=True)

    return re.sub(r"\s+", " ", text).strip()


def domain_of(url):

    try:
        return (
            urlparse(url).hostname or ""
        ).lower().removeprefix("www.")

    except Exception:
        return ""


def domain_matches(domain, known):

    return any(
        domain == item or domain.endswith("." + item)
        for item in known
    )


def normalize_url(url):

    if not url:
        return ""

    parsed = urlparse(url.strip())

    query = [
        (key, value)
        for key, value in parse_qsl(
            parsed.query,
            keep_blank_values=True
        )
        if not key.lower().startswith("utm_")
        and key.lower() not in {
            "oc",
            "fbclid",
            "gclid",
            "mc_cid",
            "mc_eid",
        }
    ]

    path = parsed.path.rstrip("/") or "/"

    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            urlencode(query),
            "",
        )
    )


# ============================================================
# DATE PROCESSING
# ============================================================

def parse_date(entry):

    for key in (
        "published",
        "updated",
        "created",
    ):

        value = entry.get(key)

        if value:

            try:
                dt = parsedate_to_datetime(value)

                if dt.tzinfo is None:
                    dt = dt.replace(
                        tzinfo=timezone.utc
                    )

                return dt.astimezone(timezone.utc)

            except Exception:
                pass

    for key in (
        "published_parsed",
        "updated_parsed",
    ):

        value = entry.get(key)

        if value:

            try:
                return datetime(
                    *value[:6],
                    tzinfo=timezone.utc
                )

            except Exception:
                pass

    return None


# ============================================================
# CHELSEA RELEVANCE FILTER
# ============================================================

def is_chelsea_relevant(title, summary):

    text = f"{title} {summary}".lower()

    if any(
        term in text
        for term in IRRELEVANT_TERMS
    ):
        return False

    return any(
        term in text
        for term in CHELSEA_TERMS
    )


# ============================================================
# STORY CLASSIFICATION
# ============================================================

def classify_story(title, summary):

    text = f"{title} {summary}".lower()

    has_transfer = any(
        term in text
        for term in TRANSFER_TERMS
    )

    has_rumour = any(
        term in text
        for term in RUMOUR_TERMS
    )

    if has_transfer and has_rumour:
        return "transfer_rumour"

    if any(
        term in text
        for term in INJURY_TERMS
    ):
        return "injury_update"

    if any(
        term in text
        for term in TEAM_TERMS
    ):
        return "team_news"

    if any(
        term in text
        for term in OPINION_TERMS
    ):
        return "opinion_analysis"

    if any(
        term in text
        for term in MATCH_TERMS
    ):
        return "match_or_competition"

    if has_transfer:
        return "transfer_update"

    return "general_news"


# ============================================================
# PUBLISHER CLASSIFICATION
# ============================================================

def publisher_type(url):

    domain = domain_of(url)

    if domain_matches(domain, OFFICIAL_DOMAINS):
        return "official_source"

    if domain_matches(domain, ESTABLISHED_DOMAINS):
        return "established_publisher"

    if domain_matches(domain, AGGREGATOR_DOMAINS):
        return "aggregator"

    return (
        "other_publisher"
        if domain
        else "publisher_unresolved"
    )


# ============================================================
# ARTICLE URL RESOLUTION
# ============================================================

def resolve_article_url(url, session):

    """
    Resolve only actual redirects or explicit canonical metadata.

    Do not select arbitrary external links from aggregator pages.
    """

    if not url:
        return "", False

    original_domain = domain_of(url)

    try:

        response = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True
        )

        final_url = response.url or url
        final_domain = domain_of(final_url)

        if (
            final_domain
            and not domain_matches(
                final_domain,
                AGGREGATOR_DOMAINS
            )
        ):

            return normalize_url(final_url), True

        soup = BeautifulSoup(
            response.text[:1_000_000],
            "html.parser"
        )

        metadata = [
            (
                "link",
                {"rel": "canonical"},
                "href",
            ),
            (
                "meta",
                {"property": "og:url"},
                "content",
            ),
            (
                "meta",
                {"name": "twitter:url"},
                "content",
            ),
        ]

        for tag_name, attributes, field in metadata:

            tag = soup.find(
                tag_name,
                attributes
            )

            if tag and tag.get(field):

                candidate = urljoin(
                    final_url,
                    tag.get(field)
                )

                candidate_domain = domain_of(candidate)

                if (
                    candidate_domain
                    and not domain_matches(
                        candidate_domain,
                        AGGREGATOR_DOMAINS
                    )
                ):

                    return normalize_url(candidate), True

    except requests.RequestException as exc:

        logging.debug(
            "Could not resolve %s: %s",
            url,
            exc
        )

    except Exception as exc:

        logging.debug(
            "URL resolution issue for %s: %s",
            url,
            exc
        )

    return (
        normalize_url(url),
        not domain_matches(
            original_domain,
            AGGREGATOR_DOMAINS
        )
    )


# ============================================================
# PUBLISHER NAME IDENTIFICATION
# ============================================================

def identify_publisher(entry, url, source_name):

    source = entry.get("source")

    if isinstance(source, dict):

        name = clean_text(
            source.get("title", "")
        )

        if name and name.lower() not in {
            "google news",
            "newsnow",
        }:
            return name[:100]

    title = clean_text(
        entry.get("title", "")
    )

    match = re.search(
        r"\s(?:- |\| )([^-|]{2,70})$",
        title
    )

    if match:

        candidate = match.group(1).strip()

        if candidate.lower() not in {
            "chelsea",
            "latest news",
        }:
            return candidate

    domain = domain_of(url)

    if (
        domain
        and not domain_matches(
            domain,
            AGGREGATOR_DOMAINS
        )
    ):
        return domain

    return source_name


# ============================================================
# STORY CREATION
# ============================================================

def make_story(
    title,
    summary,
    url,
    source_name,
    publisher_hint,
    published,
    session,
    discovery_method,
):

    title = clean_text(title)
    summary = clean_text(summary)

    if not title or not url:
        return None

    if not is_chelsea_relevant(
        title,
        summary
    ):
        return None

    if published and published < CUTOFF:
        return None

    resolved_url, resolved = resolve_article_url(
        url,
        session
    )

    final_domain = domain_of(resolved_url)

    pub_type = publisher_type(
        resolved_url
    )

    publisher = (
        publisher_hint
        or final_domain
        or "Unresolved publisher"
    )

    if (
        resolved
        and final_domain
        and not domain_matches(
            final_domain,
            AGGREGATOR_DOMAINS
        )
    ):

        if publisher.lower() in {
            "google news",
            "newsnow",
        }:
            publisher = final_domain

    if pub_type == "official_source":
        verification = "official_primary_source"

    elif pub_type == "established_publisher":
        verification = "established_publisher_unverified"

    elif pub_type == "aggregator":
        verification = "publisher_unresolved"

    else:
        verification = "unverified_source"

    published_iso = (
        published.isoformat()
        if published
        else None
    )

    title_key = re.sub(
        r"[^a-z0-9]+",
        " ",
        title.lower()
    ).strip()

    url_key = normalize_url(
        resolved_url or url
    )

    story_id = hashlib.sha256(
        (url_key or title_key).encode("utf-8")
    ).hexdigest()[:20]

    return {

        "id": story_id,

        "title_key": title_key,

        "title": title,

        "summary": summary[:1200],

        "url": resolved_url,

        "original_url": url,

        "normalized_url": url_key,

        "url_resolved": bool(resolved),

        "source": source_name,

        "publisher": publisher,

        "publisher_type": pub_type,

        "published": published_iso,

        "date_unverified": published is None,

        "story_type": classify_story(
            title,
            summary
        ),

        "verification_status": verification,

        "corroborating_publishers": [],

        "corroboration_count": 0,

        "corroboration_status": "not_checked",

        "importance_score": 0,

        "discovery_method": discovery_method,

        "previously_seen": False,

    }


# ============================================================
# RSS COLLECTION
# ============================================================

def fetch_rss(config, session):

    stories = []

    report = {

        "source": config["name"],

        "url": config["url"],

        "status": "failed",

        "entries_received": 0,

        "stories_collected": 0,

        "rejected_old": 0,

        "error": None,

    }

    try:

        response = session.get(
            config["url"],
            timeout=REQUEST_TIMEOUT
        )

        response.raise_for_status()

        feed = feedparser.parse(
            response.content
        )

        if not feed.entries:

            report["error"] = (
                "Feed returned no entries"
            )

            return stories, report

        report["entries_received"] = len(
            feed.entries
        )

        for entry in feed.entries:

            published = parse_date(entry)

            if published and published < CUTOFF:

                report["rejected_old"] += 1

                continue

            title = clean_text(
                entry.get("title", "")
            )

            summary = clean_text(
                entry.get(
                    "summary",
                    entry.get(
                        "description",
                        ""
                    )
                )
            )

            if not is_chelsea_relevant(
                title,
                summary
            ):
                continue

            link = entry.get(
                "link",
                ""
            )

            publisher = identify_publisher(
                entry,
                link,
                config["name"]
            )

            story = make_story(
                title,
                summary,
                link,
                config["name"],
                publisher,
                published,
                session,
                "rss"
            )

            if story:
                stories.append(story)

        report["status"] = "success"

        report["stories_collected"] = len(
            stories
        )

    except Exception as exc:

        report["error"] = (
            f"{type(exc).__name__}: {exc}"
        )

        logging.warning(
            "Feed failed: %s — %s",
            config["name"],
            exc
        )

    return stories, report


# ============================================================
# NEWSNOW EXTRACTION
# ============================================================

def extract_newsnow_items(html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    results = []
    seen = set()

    for anchor in soup.select("a[href]"):

        href = urljoin(
            NEWSNOW_URL,
            anchor.get("href", "")
        )

        title = clean_text(
            anchor.get_text(
                " ",
                strip=True
            )
        )

        if (
            not title
            or len(title) < 18
            or not is_chelsea_relevant(
                title,
                ""
            )
        ):
            continue

        if (
            href in seen
            or href.startswith("javascript:")
        ):
            continue

        seen.add(href)

        parent = anchor
        context = ""

        for _ in range(4):

            if parent.parent:

                parent = parent.parent

                context = clean_text(
                    parent.get_text(
                        " ",
                        strip=True
                    )
                )

                if len(context) > len(title) + 10:
                    break

        domain = domain_of(href)

        if not domain:
            continue

        is_aggregator = domain_matches(
            domain,
            AGGREGATOR_DOMAINS
        )

        # Direct publisher URLs and NewsNow links are accepted.
        if not is_aggregator and domain == "newsnow.com":
            continue

        publisher = "NewsNow"

        remainder = context.replace(
            title,
            " ",
            1
        )

        remainder = re.sub(
            r"\b(?:\d{1,2}:\d{2}|\d+\s+(?:minutes?|hours?|days?)\s+ago)\b",
            " ",
            remainder,
            flags=re.I
        )

        remainder = re.sub(
            r"\s+",
            " ",
            remainder
        ).strip(" -|·")

        if remainder and len(remainder) <= 100:
            publisher = remainder

        results.append({

            "title": title,

            "url": href,

            "summary": context[:1000],

            "publisher": publisher,

        })

        if len(results) >= MAX_NEWSNOW_ITEMS:
            break

    return results


def fetch_newsnow(session):

    stories = []

    report = {

        "source": "NewsNow",

        "url": NEWSNOW_URL,

        "status": "failed",

        "entries_received": 0,

        "stories_collected": 0,

        "error": None,

    }

    try:

        response = session.get(
            NEWSNOW_URL,
            timeout=REQUEST_TIMEOUT
        )

        response.raise_for_status()

        items = extract_newsnow_items(
            response.text
        )

        report["entries_received"] = len(
            items
        )

        for item in items:

            story = make_story(
                item["title"],
                item["summary"],
                item["url"],
                "NewsNow",
                item["publisher"],
                None,
                session,
                "newsnow"
            )

            if story:
                stories.append(story)

        report["status"] = "success"

        report["stories_collected"] = len(
            stories
        )

    except Exception as exc:

        report["error"] = (
            f"{type(exc).__name__}: {exc}"
        )

        logging.warning(
            "NewsNow collection failed: %s",
            exc
        )

    return stories, report


# ============================================================
# DUPLICATE DETECTION
# ============================================================

def similarity(a, b):

    return SequenceMatcher(
        None,
        a.lower(),
        b.lower()
    ).ratio()


def deduplicate(stories):

    groups = []

    for story in stories:

        match = None

        for existing in groups:

            same_url = (
                story.get("normalized_url")
                and story.get("normalized_url")
                == existing.get("normalized_url")
            )

            similar_title = (
                similarity(
                    story.get("title_key", ""),
                    existing.get("title_key", "")
                ) >= 0.87
            )

            if same_url or similar_title:

                match = existing

                break

        if match is None:

            groups.append(story)

            continue

        publishers = set(
            match.get(
                "corroborating_publishers",
                []
            )
        )

        publishers.add(
            match.get("publisher", "Unknown")
        )

        publishers.add(
            story.get("publisher", "Unknown")
        )

        publishers.discard("Unknown")

        match["corroborating_publishers"] = sorted(
            publishers
        )

        match["corroboration_count"] = len(
            publishers
        )

        match["corroboration_status"] = (
            "multiple_publishers_detected"
            if len(publishers) > 1
            else "single_publisher_detected"
        )

        if (
            story.get("url_resolved")
            and not match.get("url_resolved")
        ):

            for key in (
                "url",
                "normalized_url",
                "publisher",
                "publisher_type",
                "verification_status",
                "url_resolved",
            ):

                match[key] = story.get(key)

        if len(
            story.get("summary", "")
        ) > len(
            match.get("summary", "")
        ):

            match["summary"] = story["summary"]

    for story in groups:

        if story.get("corroboration_count", 0) == 0:

            publisher = story.get(
                "publisher",
                "Unknown"
            )

            story["corroborating_publishers"] = (
                [publisher]
                if publisher != "Unknown"
                else []
            )

            story["corroboration_count"] = len(
                story["corroborating_publishers"]
            )

            story["corroboration_status"] = (
                "single_publisher_detected"
                if publisher != "Unknown"
                else "not_established"
            )

    return groups


# ============================================================
# IMPORTANCE SCORING
# ============================================================

def importance_score(story):

    text = (
        f"{story.get('title', '')} "
        f"{story.get('summary', '')}"
    ).lower()

    score = 10

    if story.get("publisher_type") == "official_source":
        score += 35

    elif story.get("publisher_type") == "established_publisher":
        score += 20

    if story.get("corroboration_count", 0) >= 3:
        score += 15

    elif story.get("corroboration_count", 0) == 2:
        score += 8

    if story.get("story_type") in {
        "injury_update",
        "team_news",
        "transfer_update",
        "transfer_rumour",
    }:
        score += 12

    if any(
        term in text
        for term in (
            "confirmed",
            "official",
            "announced",
            "signs",
            "signed",
        )
    ):
        score += 8

    if story.get("date_unverified"):
        score -= 5

    if not story.get("url_resolved"):
        score -= 8

    return max(
        0,
        min(100, score)
    )


# ============================================================
# HISTORY MANAGEMENT
# ============================================================

def load_history():

    if not HISTORY_FILE.exists():
        return {}

    try:

        data = json.loads(
            HISTORY_FILE.read_text(
                encoding="utf-8"
            )
        )

        if (
            isinstance(data, dict)
            and isinstance(data.get("stories"), dict)
        ):
            return data["stories"]

        # Backward compatibility with v3.2.
        if isinstance(data, dict):
            return data

    except Exception as exc:

        logging.warning(
            "Could not read history file; starting empty: %s",
            exc
        )

    return {}


def history_keys(story):

    keys = []

    if story.get("normalized_url"):

        keys.append(
            "url:" + hashlib.sha256(
                story["normalized_url"].encode()
            ).hexdigest()
        )

    if story.get("title_key"):

        keys.append(
            "title:" + hashlib.sha256(
                story["title_key"].encode()
            ).hexdigest()
        )

    return keys


def apply_history(stories, history):

    now = datetime.now(timezone.utc)

    cutoff = now - timedelta(
        days=HISTORY_DAYS
    )

    cleaned = {}

    for key, value in history.items():

        timestamp = (
            value.get("last_seen")
            if isinstance(value, dict)
            else value
        )

        try:

            dt = datetime.fromisoformat(
                str(timestamp).replace(
                    "Z",
                    "+00:00"
                )
            )

            if dt.tzinfo is None:

                dt = dt.replace(
                    tzinfo=timezone.utc
                )

            if dt >= cutoff:

                cleaned[key] = {
                    "last_seen": dt.astimezone(
                        timezone.utc
                    ).isoformat()
                }

        except Exception:
            continue

    new_stories = []

    for story in stories:

        keys = history_keys(story)

        story["previously_seen"] = any(
            key in cleaned
            for key in keys
        )

        if not story["previously_seen"]:
            new_stories.append(story)

        for key in keys:

            cleaned[key] = {

                "last_seen": now.isoformat(),

                "title": story.get(
                    "title",
                    ""
                )[:180],

            }

    if len(cleaned) > MAX_HISTORY_ITEMS:

        cleaned = dict(
            list(cleaned.items())[-MAX_HISTORY_ITEMS:]
        )

    return new_stories, cleaned


# ============================================================
# JSON OUTPUT
# ============================================================

def write_json(path, data):

    path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


# ============================================================
# MAIN COLLECTOR
# ============================================================

def main():

    started = datetime.now(timezone.utc)

    session = requests.Session()

    session.headers.update(HEADERS)

    all_stories = []

    source_reports = []

    # Collect RSS feeds.
    for config in FEEDS:

        logging.info(
            "Collecting: %s",
            config["name"]
        )

        stories, report = fetch_rss(
            config,
            session
        )

        all_stories.extend(stories)

        source_reports.append(report)

        time.sleep(0.25)

    # Collect NewsNow.
    logging.info("Collecting: NewsNow")

    stories, report = fetch_newsnow(
        session
    )

    all_stories.extend(stories)

    source_reports.append(report)

    # Remove duplicate stories.
    unique_stories = deduplicate(
        all_stories
    )

    # Load persistent history.
    history = load_history()

    new_stories, updated_history = apply_history(
        unique_stories,
        history
    )

    # Score and rank new candidates.
    for story in new_stories:

        story["importance_score"] = importance_score(
            story
        )

    new_stories.sort(
        key=lambda item: (
            item.get("importance_score", 0),
            item.get("published") or "",
        ),
        reverse=True
    )

    selected = new_stories[:MAX_STORIES]

    # Collection report.
    report_data = {

        "collector_version": VERSION,

        "run_started_at": started.isoformat(),

        "run_completed_at": datetime.now(
            timezone.utc
        ).isoformat(),

        "configured_sources": len(FEEDS) + 1,

        "successful_sources": sum(
            1
            for item in source_reports
            if item["status"] == "success"
        ),

        "failed_sources": sum(
            1
            for item in source_reports
            if item["status"] != "success"
        ),

        "total_collected": len(all_stories),

        "unique_stories": len(unique_stories),

        "new_stories": len(new_stories),

        "selected_stories": len(selected),

        "unresolved_article_urls": sum(
            1
            for item in selected
            if not item.get("url_resolved")
        ),

        "source_reports": source_reports,

        "notes": [

            "Collected items are candidates, not independently verified facts.",

            "Multiple publisher labels do not prove independent corroboration.",

            "Aggregator URLs remain unresolved unless a redirect or canonical URL identifies the publisher article.",

            "No article generation or Blogger/X publishing is performed by this collector.",

        ],

    }

    candidates_data = {

        "collector_version": VERSION,

        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),

        "stories": selected,

    }

    history_data = {

        "history_version": VERSION,

        "updated_at": datetime.now(
            timezone.utc
        ).isoformat(),

        "stories": updated_history,

    }

    # Save all three output files.
    write_json(
        CANDIDATES_FILE,
        candidates_data
    )

    write_json(
        REPORT_FILE,
        report_data
    )

    write_json(
        HISTORY_FILE,
        history_data
    )

    logging.info(
        "Collection complete: %s collected, %s new, %s selected",
        len(all_stories),
        len(new_stories),
        len(selected)
    )

    logging.info(
        "Report saved to %s",
        REPORT_FILE.name
    )

    return 0


if __name__ == "__main__":

    raise SystemExit(main())
