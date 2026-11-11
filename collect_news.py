import feedparser
import requests
import json
import re
import hashlib
import html
import calendar

from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from urllib.parse import urlparse, urlunparse


# ==========================================
# CHELSEAFEEDS NEWS COLLECTOR VERSION 3.1
# ==========================================

VERSION = "3.1"
MAX_AGE_DAYS = 7
MAX_STORIES = 50

NOW = datetime.now(timezone.utc)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
    )
}


# ==========================================
# TEN NEWS COLLECTION SOURCES
# ==========================================

FEEDS = [

    {
        "name": "BBC Sport",
        "url": "https://feeds.bbci.co.uk/sport/football/rss.xml"
    },

    {
        "name": "Sky Sports",
        "url": "https://www.skysports.com/rss/12040"
    },

    {
        "name": "Google News - Chelsea",
        "url": (
            "https://news.google.com/rss/search?"
            "q=Chelsea+FC+when:7d"
            "&hl=en-GB&gl=GB&ceid=GB:en"
        )
    },

    {
        "name": "The Guardian - Chelsea",
        "url": "https://www.theguardian.com/football/chelsea/rss"
    },

    {
        "name": "TalkChelsea",
        "url": "https://www.talkchelsea.net/feed/"
    },

    {
        "name": "Chelsea.news",
        "url": "https://chelsea.news/feed"
    },

    {
        "name": "Chelsea FC Latest News",
        "url": "https://chelseafclatestnews.com/feed"
    },

    {
        "name": "Football365",
        "url": "https://www.football365.com/rss"
    },

    {
        "name": "Google News - Official Chelsea",
        "url": (
            "https://news.google.com/rss/search?"
            "q=site%3Achealseafc.com%2Fen%2Fnews+Chelsea"
            "&hl=en-GB&gl=GB&ceid=GB:en"
        )
    }

]

NEWSNOW_URL = (
    "https://www.newsnow.co.uk/h/Sport/Football/"
    "Premier%20League/Chelsea"
)


# ==========================================
# FILTERING KEYWORDS
# ==========================================

CHELSEA_TERMS = [
    "chelsea",
    "stamford bridge",
    "the blues",
    "chelseafc",
    "chelsea fc"
]

FOOTBALL_TERMS = [
    "football",
    "premier league",
    "transfer",
    "goal",
    "manager",
    "coach",
    "striker",
    "defender",
    "midfielder",
    "match",
    "fixture",
    "cup",
    "champions league",
    "injury",
    "squad",
    "player",
    "signing",
    "loan",
    "academy"
]

LOW_VALUE_TERMS = [
    "casino",
    "betting tips",
    "gambling",
    "horoscope",
    "lottery"
]

RUMOUR_TERMS = [
    "rumour",
    "rumor",
    "linked with",
    "interested in",
    "could sign",
    "reportedly",
    "keen on",
    "transfer target",
    "considering a move",
    "set to sign",
    "transfer speculation"
]

OPINION_TERMS = [
    "opinion",
    "should sign",
    "should sell",
    "why chelsea must",
    "explained",
    "player ratings",
    "pundit"
]


# ==========================================
# TEXT CLEANING
# ==========================================

def clean_text(value):

    value = html.unescape(str(value or ""))

    value = BeautifulSoup(
        value,
        "html.parser"
    ).get_text(" ")

    return re.sub(
        r"\s+",
        " ",
        value
    ).strip()


# ==========================================
# DATE HANDLING
# ==========================================

def parse_date(entry):

    for key in (
        "published_parsed",
        "updated_parsed"
    ):

        value = entry.get(key)

        if value:

            try:

                timestamp = calendar.timegm(value)

                return datetime.fromtimestamp(
                    timestamp,
                    timezone.utc
                )

            except Exception:
                pass

    return None


# ==========================================
# URL NORMALIZATION
# ==========================================

def normalize_url(url):

    try:

        parsed = urlparse(url)

        if not parsed.scheme or not parsed.netloc:
            return url

        return urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            "",
            "",
            ""
        ))

    except Exception:

        return url


# ==========================================
# CHELSEA RELEVANCE FILTER
# ==========================================

def is_relevant(title, summary=""):

    text = f"{title} {summary}".lower()

    has_chelsea = any(
        term in text
        for term in CHELSEA_TERMS
    )

    has_football = any(
        term in text
        for term in FOOTBALL_TERMS
    )

    if not has_chelsea:
        return False

    if not has_football:
        return False

    if any(
        term in text
        for term in LOW_VALUE_TERMS
    ):
        return False

    return True


# ==========================================
# STORY CLASSIFICATION
# ==========================================

def classify_story(title):

    title_lower = title.lower()

    if any(
        term in title_lower
        for term in RUMOUR_TERMS
    ):

        return "rumour"

    if any(
        term in title_lower
        for term in OPINION_TERMS
    ):

        return "opinion"

    return "news_candidate"


# ==========================================
# RELEVANCE SCORING
# ==========================================

def relevance_score(title, summary):

    text = f"{title} {summary}".lower()

    score = 0

    important_terms = [
        "chelsea",
        "transfer",
        "injury",
        "manager",
        "premier league",
        "champions league",
        "signing",
        "official",
        "confirmed",
        "match report",
        "squad",
        "contract",
        "goal",
        "fixture"
    ]

    for term in important_terms:

        if term in text:
            score += 2

    if "chelsea" in title.lower():
        score += 5

    if "stamford bridge" in title.lower():
        score += 3

    return score


# ==========================================
# STORY IDENTIFICATION
# ==========================================

def create_id(title, url):

    raw = (
        title.lower().strip()
        + normalize_url(url)
    ).encode("utf-8")

    return hashlib.sha256(
        raw
    ).hexdigest()[:16]


# ==========================================
# DUPLICATE DETECTION
# ==========================================

def title_similarity(first, second):

    return SequenceMatcher(
        None,
        first.lower(),
        second.lower()
    ).ratio()


# ==========================================
# RSS COLLECTION
# ==========================================

def collect_rss(feed):

    results = []

    report = {
        "source": feed["name"],
        "type": "RSS",
        "status": "failed",
        "stories_found": 0,
        "error": None
    }

    try:

        response = requests.get(
            feed["url"],
            headers=HEADERS,
            timeout=25
        )

        response.raise_for_status()

        parsed = feedparser.parse(
            response.content
        )

        if not parsed.entries:

            raise ValueError(
                "Feed returned no entries"
            )

        report["status"] = "success"

        for entry in parsed.entries:

            title = clean_text(
                entry.get("title", "")
            )

            summary = clean_text(
                entry.get(
                    "summary",
                    entry.get("description", "")
                )
            )

            url = entry.get("link", "")

            if not title or not url:
                continue

            if not is_relevant(title, summary):
                continue

            published = parse_date(entry)

            if published:

                if published < NOW - timedelta(
                    days=MAX_AGE_DAYS
                ):
                    continue

                if published > NOW + timedelta(days=1):
                    continue

            publisher = feed["name"]

            if entry.get("source"):

                publisher = clean_text(
                    entry.source.get(
                        "title",
                        publisher
                    )
                )

            results.append({

                "title": title,

                "summary": summary[:1500],

                "url": url,

                "normalized_url": normalize_url(url),

                "source": feed["name"],

                "publisher": publisher,

                "published": (
                    published.isoformat()
                    if published else None
                ),

                "date_unverified": published is None,

                "story_type": classify_story(title),

                "verification_status": "not_verified",

                "relevance_score": relevance_score(
                    title,
                    summary
                ),

                "discovery_method": "RSS"

            })

        report["stories_found"] = len(results)

    except Exception as error:

        report["status"] = "failed"

        report["error"] = str(error)[:300]

    return results, report


# ==========================================
# NEWSNOW WEBPAGE COLLECTION
# ==========================================

def collect_newsnow():

    results = []

    report = {

        "source": "NewsNow Chelsea",

        "type": "Webpage discovery",

        "status": "failed",

        "stories_found": 0,

        "error": None

    }

    try:

        response = requests.get(
            NEWSNOW_URL,
            headers=HEADERS,
            timeout=25
        )

        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        seen = set()

        for link in soup.find_all(
            "a",
            href=True
        ):

            title = clean_text(
                link.get_text(" ", strip=True)
            )

            href = link["href"]

            if len(title) < 25 or len(title) > 250:
                continue

            if href.startswith("/"):
                continue

            parsed = urlparse(href)

            if not parsed.scheme:
                continue

            if "newsnow.co.uk" in parsed.netloc.lower():
                continue

            # Examine nearby article-card text.
            surrounding_text = ""

            parent = link

            for _ in range(3):

                if parent.parent:

                    parent = parent.parent

                    parent_text = clean_text(
                        parent.get_text(" ", strip=True)
                    )

                    # Avoid using the entire webpage
                    # as the context for every headline.
                    if len(parent_text) <= 600:

                        surrounding_text = parent_text

                        break

            if not is_relevant(
                title,
                surrounding_text
            ):
                continue

            normalized = normalize_url(href)

            if normalized in seen:
                continue

            seen.add(normalized)

            results.append({

                "title": title,

                "summary": "",

                "url": href,

                "normalized_url": normalized,

                "source": "NewsNow Chelsea",

                "publisher": "Publisher not yet verified",

                "published": None,

                "date_unverified": True,

                "story_type": classify_story(title),

                "verification_status": "not_verified",

                "relevance_score": relevance_score(
                    title,
                    ""
                ),

                "discovery_method": "NewsNow webpage"

            })

        report["stories_found"] = len(results)

        if results:

            report["status"] = "success"

        else:

            report["status"] = "empty"

            report["error"] = (
                "Page loaded, but no matching external "
                "article links were found. "
                "NewsNow extraction may need adjustment."
            )

    except Exception as error:

        report["status"] = "failed"

        report["error"] = str(error)[:300]

    return results, report


# ==========================================
# REMOVE DUPLICATES
# ==========================================

def remove_duplicates(stories):

    unique = []

    duplicate_count = 0

    stories.sort(
        key=lambda item: item["relevance_score"],
        reverse=True
    )

    for story in stories:

        duplicate = False

        for existing in unique:

            same_url = (
                story["normalized_url"]
                == existing["normalized_url"]
            )

            similar_title = (
                title_similarity(
                    story["title"],
                    existing["title"]
                ) >= 0.82
            )

            if same_url or similar_title:

                duplicate = True

                duplicate_count += 1

                break

        if not duplicate:

            story["id"] = create_id(
                story["title"],
                story["url"]
            )

            unique.append(story)

    return unique, duplicate_count


# ==========================================
# MAIN COLLECTION PROCESS
# ==========================================

def main():

    all_stories = []

    source_reports = []

    configured_sources = len(FEEDS) + 1

    print(
        f"ChelseaFeeds collector version {VERSION}"
    )

    print(
        f"Starting collection from {configured_sources} sources..."
    )

    # Collect RSS feeds

    for feed in FEEDS:

        stories, report = collect_rss(feed)

        all_stories.extend(stories)

        source_reports.append(report)

        print(
            f'{feed["name"]}: '
            f'{report["status"]} '
            f'({report["stories_found"]} stories)'
        )

    # Collect NewsNow

    newsnow_stories, newsnow_report = collect_newsnow()

    all_stories.extend(newsnow_stories)

    source_reports.append(newsnow_report)

    print(
        f'NewsNow Chelsea: '
        f'{newsnow_report["status"]} '
        f'({newsnow_report["stories_found"]} stories)'
    )

    # Remove duplicates

    unique_stories, duplicate_count = remove_duplicates(
        all_stories
    )

    # Apply maximum story limit

    unique_stories = unique_stories[:MAX_STORIES]

    # Count source statuses separately

    successful_sources = sum(
        1 for item in source_reports
        if item["status"] == "success"
    )

    failed_sources = sum(
        1 for item in source_reports
        if item["status"] == "failed"
    )

    empty_sources = sum(
        1 for item in source_reports
        if item["status"] == "empty"
    )

    # Prepare output

    output = {

        "collector_version": VERSION,

        "checked_at": NOW.isoformat(),

        "maximum_age_days": MAX_AGE_DAYS,

        "configured_sources": configured_sources,

        "successful_sources": successful_sources,

        "failed_sources": failed_sources,

        "empty_sources": empty_sources,

        "total_collected": len(all_stories),

        "unique_stories": len(unique_stories),

        "duplicates_removed": duplicate_count,

        "stories": unique_stories,

        "source_report": source_reports,

        "publishing_note": (
            "All stories are discovery candidates only. "
            "Verification is required before article generation."
        )

    }

    # Save JSON file

    with open(
        "news_candidates.json",
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            output,
            file,
            indent=2,
            ensure_ascii=False
        )

    # Print final report

    print("\n========== COLLECTION REPORT ==========")

    print(
        f"Configured sources: {configured_sources}"
    )

    print(
        f"Successful sources: {successful_sources}"
    )

    print(
        f"Failed sources: {failed_sources}"
    )

    print(
        f"Empty sources: {empty_sources}"
    )

    print(
        f"Total collected: {len(all_stories)}"
    )

    print(
        f"Unique stories: {len(unique_stories)}"
    )

    print(
        f"Duplicates removed: {duplicate_count}"
    )

    print("=======================================")

    print(
        "\nCollection report saved to news_candidates.json"
    )


if __name__ == "__main__":

    main()
