import feedparser
import requests
import json
import re
import hashlib
import time
import logging

from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from urllib.parse import (
    urlparse, urlunparse, parse_qsl, urlencode, urljoin
)
from difflib import SequenceMatcher
from collections import defaultdict
from email.utils import parsedate_to_datetime
from calendar import timegm


# ============================================================
# CHELSEAFEEDS COLLECTOR V3.3
# ============================================================

VERSION = "3.3"

MAX_AGE_DAYS = 7
MAX_STORIES = 50
MAX_HISTORY = 5000
HISTORY_DAYS = 60

REQUEST_TIMEOUT = 20
MAX_REDIRECT_CHECKS = 100

NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(days=MAX_AGE_DAYS)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("ChelseaFeeds")


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ChelseaFeedsCollector/3.3; "
        "+https://www.chelseafeeds.com)"
    ),
    "Accept": (
        "application/rss+xml, application/xml, "
        "text/xml, text/html, */*"
    )
}

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# NEWS SOURCES
# ============================================================

FEEDS = [

    {
        "name": "BBC Sport",
        "type": "rss",
        "url": "https://feeds.bbci.co.uk/sport/football/rss.xml"
    },

    {
        "name": "Sky Sports",
        "type": "rss",
        "url": "https://www.skysports.com/rss/12040"
    },

    {
        "name": "Google News - Chelsea",
        "type": "rss",
        "url": (
            "https://news.google.com/rss/search?"
            "q=Chelsea+FC+when:7d"
            "&hl=en-GB&gl=GB&ceid=GB:en"
        )
    },

    {
        "name": "The Guardian - Chelsea",
        "type": "rss",
        "url": "https://www.theguardian.com/football/chelsea/rss"
    },

    {
        "name": "TalkChelsea",
        "type": "rss",
        "url": "https://www.talkchelsea.net/feed/"
    },

    {
        "name": "Chelsea.news",
        "type": "rss",
        "url": "https://chelsea.news/feed"
    },

    {
        "name": "Chelsea FC Latest News",
        "type": "rss",
        "url": "https://chelseafclatestnews.com/feed"
    },

    {
        "name": "Football365",
        "type": "rss",
        "url": "https://www.football365.com/rss"
    },

    {
        "name": "Google News - Official Chelsea",
        "type": "rss",
        "url": (
            "https://news.google.com/rss/search?"
            "q=site%3Achelseafc.com%2Fen%2Fnews"
            "&hl=en-GB&gl=GB&ceid=GB:en"
        )
    },

    {
        "name": "NewsNow Chelsea",
        "type": "newsnow",
        "url": "https://www.newsnow.com/us/?search=Chelsea"
    }

]


# ============================================================
# OFFICIAL PUBLISHER DOMAINS
# ============================================================

OFFICIAL_DOMAINS = {
    "chelseafc.com",
    "premierleague.com",
    "uefa.com",
    "thefa.com"
}


TRUSTED_NEWS_DOMAINS = {
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
    "football365.com"
}


AGGREGATOR_DOMAINS = {
    "news.google.com",
    "newsnow.com",
    "c.newsnow.com"
}


# ============================================================
# RELEVANCE AND CLASSIFICATION TERMS
# ============================================================

CHELSEA_TERMS = [
    "chelsea",
    "chelsea fc",
    "chelsea women",
    "chelsea women fc",
    "chelsea football club",
    "the blues",
    "stamford bridge",
    "cobham",
    "chelsea academy",
    "chelsea u21",
    "chelsea u18"
]


IRRELEVANT_TERMS = [
    "chelsea handler",
    "chelsea clinton",
    "chelsea manning",
    "chelsea boots",
    "chelsea flower",
    "chelsea neighbourhood",
    "chelsea hotel"
]


TRANSFER_TERMS = [
    "transfer",
    "signing",
    "sign",
    "signed",
    "striker",
    "midfielder",
    "defender",
    "goalkeeper",
    "contract",
    "bid",
    "deal",
    "loan"
]


RUMOUR_TERMS = [
    "rumour",
    "rumor",
    "reportedly",
    "linked with",
    "interested in",
    "could sign",
    "may sign",
    "transfer target",
    "transfer talks",
    "bid rejected",
    "bid submitted",
    "set to sign",
    "deal close",
    "considering signing",
    "pursuit",
    "monitoring",
    "wants to sign",
    "keen on",
    "scout claims",
    "claims former",
    "according to reports"
]


OPINION_TERMS = [
    "opinion",
    "column",
    "analysis",
    "explained",
    "why",
    "should",
    "player ratings",
    "power rankings",
    "predicted",
    "prediction",
    "tactical analysis",
    "former scout claims",
    "claims former scout"
]


MATCH_TERMS = [
    "match report",
    "post-match",
    "full-time",
    "goalless draw",
    "match reaction",
    "premier league report",
    "champions league report",
    "won against",
    "defeated",
    "draw with"
]


INJURY_TERMS = [
    "injury",
    "injured",
    "fitness",
    "fitness update",
    "ruled out",
    "returns from injury",
    "medical assessment",
    "hamstring",
    "knee injury",
    "ankle injury"
]


TEAM_NEWS_TERMS = [
    "lineup",
    "line-up",
    "starting xi",
    "starting eleven",
    "squad",
    "travelling squad",
    "team news",
    "press conference",
    "availability",
    "suspended"
]


IMPORTANT_TERMS = [
    "official",
    "confirmed",
    "signs",
    "signed",
    "appointed",
    "sacked",
    "manager",
    "injury",
    "injured",
    "returns",
    "transfer",
    "contract",
    "suspension",
    "premier league",
    "champions league",
    "fa cup",
    "carabao cup",
    "final",
    "semi-final",
    "quarter-final",
    "match result",
    "wins",
    "defeat",
    "draw",
    "fixture",
    "lineup",
    "squad"
]


# ============================================================
# TEXT UTILITIES
# ============================================================

def clean_text(value):

    if not value:
        return ""

    value = BeautifulSoup(
        str(value), "html.parser"
    ).get_text(" ", strip=True)

    value = re.sub(r"\s+", " ", value)

    return value.strip()


def normalise_title(title):

    title = clean_text(title).lower()

    title = re.sub(
        r"\s*[-|]\s*(official site|chelsea football club)$",
        "",
        title
    )

    title = re.sub(
        r"[^a-z0-9 ]",
        " ",
        title
    )

    return re.sub(r"\s+", " ", title).strip()


def hash_value(value):

    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


# ============================================================
# DATE PROCESSING
# ============================================================

def parse_date(entry):

    for key in ("published_parsed", "updated_parsed"):

        parsed = entry.get(key)

        if parsed:

            try:
                return datetime.fromtimestamp(
                    timegm(parsed),
                    timezone.utc
                )

            except Exception:
                pass

    for key in ("published", "updated", "date"):

        value = entry.get(key)

        if value:

            try:
                result = parsedate_to_datetime(value)

                if result.tzinfo is None:
                    result = result.replace(
                        tzinfo=timezone.utc
                    )

                return result.astimezone(timezone.utc)

            except Exception:
                pass

    return None


# ============================================================
# URL PROCESSING
# ============================================================

def domain_of(url):

    try:

        host = urlparse(url).hostname or ""

        return host.lower().removeprefix("www.")

    except Exception:

        return ""


def is_domain(domain, registered_domain):

    return (
        domain == registered_domain
        or domain.endswith("." + registered_domain)
    )


def is_aggregator(url):

    domain = domain_of(url)

    return any(
        is_domain(domain, item)
        for item in AGGREGATOR_DOMAINS
    )


def normalize_url(url):

    if not url:
        return ""

    try:

        parsed = urlparse(url.strip())

        if not parsed.scheme or not parsed.netloc:
            return url.strip()

        ignored_parameters = {
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            "fbclid",
            "gclid",
            "mc_cid",
            "mc_eid",
            "oc"
        }

        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query)
            if key.lower() not in ignored_parameters
        ]

        path = re.sub(
            r"/+$",
            "",
            parsed.path
        )

        return urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            urlencode(query),
            ""
        ))

    except Exception:

        return url


# ============================================================
# ORIGINAL ARTICLE URL RESOLUTION
# ============================================================

def extract_canonical(soup, base_url):

    canonical = soup.find(
        "link",
        attrs={
            "rel": lambda value:
            value and "canonical" in value
        }
    )

    if canonical and canonical.get("href"):

        return urljoin(
            base_url,
            canonical["href"]
        )

    for attribute in (
        "og:url",
        "twitter:url"
    ):

        tag = soup.find(
            "meta",
            attrs={"property": attribute}
        )

        if tag and tag.get("content"):

            return urljoin(
                base_url,
                tag["content"]
            )

    return None


def resolve_article_url(url):

    if not url:
        return url, False

    if not is_aggregator(url):

        return url, True

    try:

        response = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True
        )

        final_url = response.url

        if final_url and not is_aggregator(final_url):

            if final_url.startswith("http"):

                return final_url, True

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        canonical = extract_canonical(
            soup,
            final_url
        )

        if canonical and not is_aggregator(canonical):

            return canonical, True

        # Some redirect pages expose the original link
        # through metadata or an ordinary anchor.

        for anchor in soup.find_all(
            "a",
            href=True
        ):

            candidate = urljoin(
                final_url,
                anchor["href"]
            )

            domain = domain_of(candidate)

            if (
                candidate.startswith("http")
                and not is_aggregator(candidate)
                and domain
            ):

                return candidate, True

    except Exception as error:

        log.warning(
            "URL resolution failed: %s",
            str(error)[:150]
        )

    # Preserve the discovery URL if resolution fails.
    return url, False


# ============================================================
# PUBLISHER IDENTIFICATION
# ============================================================

def identify_publisher(entry, url, feed_name):

    # RSS source metadata is often the best publisher hint.

    source = entry.get("source", {})

    if isinstance(source, dict):

        name = clean_text(
            source.get("title", "")
        )

        if name:
            return name

    # Google News often adds publisher after the headline.

    title = clean_text(
        entry.get("title", "")
    )

    suffix = re.search(
        r"\s[-|]\s([^|]+)$",
        title
    )

    if suffix:

        possible_name = clean_text(
            suffix.group(1)
        )

        if possible_name and len(possible_name) < 80:

            return possible_name

    domain = domain_of(url)

    if domain:

        return domain

    return feed_name


def publisher_classification(url):

    domain = domain_of(url)

    if any(
        is_domain(domain, item)
        for item in OFFICIAL_DOMAINS
    ):

        return "official"

    if any(
        is_domain(domain, item)
        for item in TRUSTED_NEWS_DOMAINS
    ):

        return "established_publisher"

    if is_aggregator(url):

        return "aggregator_unresolved"

    return "other_publisher"


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

def classify_story(title, summary, url):

    text = f"{title} {summary}".lower()

    domain = domain_of(url)

    official = any(
        is_domain(domain, item)
        for item in OFFICIAL_DOMAINS
    )

    has_transfer_term = any(
        term in text
        for term in TRANSFER_TERMS
    )

    has_rumour_term = any(
        term in text
        for term in RUMOUR_TERMS
    )

    has_opinion_term = any(
        term in text
        for term in OPINION_TERMS
    )

    has_match_term = any(
        term in text
        for term in MATCH_TERMS
    )

    has_injury_term = any(
        term in text
        for term in INJURY_TERMS
    )

    has_team_news_term = any(
        term in text
        for term in TEAM_NEWS_TERMS
    )

    if has_transfer_term and has_rumour_term:

        story_type = "transfer_rumour"

    elif has_opinion_term:

        story_type = "opinion_analysis"

    elif has_injury_term:

        story_type = "injury_news"

    elif has_team_news_term:

        story_type = "team_news"

    elif has_match_term:

        story_type = "match_report"

    elif has_transfer_term:

        story_type = "transfer_update"

    elif official:

        story_type = "official_announcement"

    else:

        story_type = "general_news"

    if official:

        verification_status = "official_primary_source"

    elif publisher_classification(url) == "established_publisher":

        verification_status = "established_publisher_unverified"

    else:

        verification_status = "not_verified"

    return story_type, verification_status


# ============================================================
# IMPORTANCE SCORING
# ============================================================

def importance_score(
    title,
    summary,
    url,
    published,
    story_type
):

    text = f"{title} {summary}".lower()

    score = 0

    if "chelsea fc" in text:
        score += 3

    elif "chelsea" in text:
        score += 2

    for term in IMPORTANT_TERMS:

        if term in text:
            score += 2

    publisher_type = publisher_classification(url)

    if publisher_type == "official":
        score += 5

    elif publisher_type == "established_publisher":
        score += 3

    if story_type == "transfer_rumour":
        score -= 2

    if story_type == "opinion_analysis":
        score -= 3

    if published:

        age_hours = (
            NOW - published
        ).total_seconds() / 3600

        if age_hours <= 12:
            score += 5

        elif age_hours <= 24:
            score += 4

        elif age_hours <= 48:
            score += 3

        elif age_hours <= 96:
            score += 2

        else:
            score += 1

    return max(0, min(score, 40))


# ============================================================
# STORY CREATION
# ============================================================

def make_story(
    title,
    summary,
    original_url,
    source,
    published=None,
    discovery_method="rss",
    publisher_hint=None
):

    title = clean_text(title)

    summary = clean_text(summary)

    if not title or not original_url:
        return None

    if not is_chelsea_relevant(title, summary):
        return None

    resolved_url, resolved = resolve_article_url(
        original_url
    )

    publisher = (
        publisher_hint
        or domain_of(resolved_url)
        or source
    )

    story_type, verification_status = classify_story(
        title,
        summary,
        resolved_url
    )

    # If a Google/NewsNow destination could not be resolved,
    # do not pretend that the publisher has been verified.

    if not resolved and is_aggregator(resolved_url):

        verification_status = "publisher_unresolved"

    normalized_url = normalize_url(
        resolved_url
    )

    title_key = hash_value(
        normalise_title(title)
    )

    story_identifier = hash_value(
        normalized_url or title_key
    )

    return {

        "id": story_identifier,

        "title_key": title_key,

        "title": title,

        "summary": summary[:2000],

        "url": resolved_url,

        "original_url": original_url,

        "normalized_url": normalized_url,

        "url_resolved": resolved,

        "source": source,

        "publisher": publisher,

        "publisher_type": publisher_classification(
            resolved_url
        ),

        "published": (
            published.isoformat()
            if published else None
        ),

        "date_unverified": published is None,

        "story_type": story_type,

        "verification_status": verification_status,

        "corroborating_publishers": [],

        "corroboration_count": 1,

        "corroboration_status": (
            "single_publisher"
        ),

        "importance_score": importance_score(
            title,
            summary,
            resolved_url,
            published,
            story_type
        ),

        "discovery_method": discovery_method,

        "previously_seen": False

    }


# ============================================================
# RSS COLLECTION
# ============================================================

def collect_rss(config):

    stories = []

    report = {

        "source": config["name"],

        "type": "RSS",

        "status": "failed",

        "entries_received": 0,

        "stories_found": 0,

        "rejected_old": 0,

        "rejected_irrelevant": 0,

        "unresolved_links": 0,

        "error": None

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
                    entry.get("description", "")
               
