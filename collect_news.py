import feedparser
import json
import re
import html
import calendar
import hashlib
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from urllib.parse import urlparse, parse_qs, unquote


# =====================================================
# CHELSEAFEEDS NEWS COLLECTOR - VERSION 2.0
# =====================================================

VERSION = "2.0"

MAX_AGE_DAYS = 7
MAX_STORIES = 50

OUTPUT_FILE = "news_candidates.json"

RSS_FEEDS = {
    "BBC Sport": (
        "https://feeds.bbci.co.uk/sport/football/rss.xml"
    ),
    "Sky Sports": (
        "https://www.skysports.com/rss/12040"
    ),
    "Google News Chelsea": (
        "https://news.google.com/rss/search?q=Chelsea+FC+when:7d&hl=en-GB&gl=GB&ceid=GB:en"
    ),
}

CHELSEA_TERMS = [
    "chelsea fc",
    "chelsea football club",
    "chelsea",
    "stamford bridge",
    "the blues",
]

FOOTBALL_TERMS = [
    "football",
    "premier league",
    "fa cup",
    "carabao cup",
    "champions league",
    "europa league",
    "conference league",
    "transfer",
    "manager",
    "coach",
    "striker",
    "midfielder",
    "defender",
    "goalkeeper",
    "squad",
    "fixture",
    "match",
    "goal",
    "injury",
    "contract",
    "footballer",
    "premier",
    "uefa",
]

IMPORTANT_TERMS = [
    "transfer",
    "signing",
    "signed",
    "official",
    "confirmed",
    "injury",
    "injured",
    "returns",
    "return",
    "manager",
    "appointment",
    "sacked",
    "departure",
    "leaves",
    "contract",
    "premier league",
    "champions league",
    "match report",
    "lineup",
    "starting xi",
    "result",
    "victory",
    "defeat",
    "draw",
    "goal",
    "suspension",
    "ban",
    "academy",
    "youth",
    "loan",
    "record",
]

LOW_VALUE_TERMS = [
    "quiz",
    "crossword",
    "fantasy football",
    "predicted lineup",
    "predicted line-up",
    "player ratings",
    "how to watch",
    "where to watch",
    "live stream",
    "tickets",
    "giveaway",
]


def clean_text(value):
    """Remove HTML and normalize text."""

    if not value:
        return ""

    value = html.unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def parse_date(entry):
    """Convert RSS publication date into UTC."""

    date_struct = (
        entry.get("published_parsed")
        or entry.get("updated_parsed")
    )

    if not date_struct:
        return None

    try:
        timestamp = calendar.timegm(date_struct)

        return datetime.fromtimestamp(
            timestamp,
            tz=timezone.utc
        )

    except (ValueError, TypeError, OverflowError):
        return None


def normalize_url(url):
    """Normalize URLs and unwrap common Google News links."""

    if not url:
        return ""

    url = html.unescape(url.strip())

    try:
        parsed = urlparse(url)

        if "news.google.com" in parsed.netloc:
            query = parse_qs(parsed.query)

            if query.get("url"):
                url = query["url"][0]
            elif "/articles/" in parsed.path:
                url = url

        parsed = urlparse(url)

        return (
            parsed.netloc.lower().replace("www.", "")
            + parsed.path.rstrip("/")
        )

    except Exception:
        return url.lower().rstrip("/")


def get_source_name(feed_name, entry):
    """Identify the publisher."""

    source = entry.get("source", {})

    if isinstance(source, dict) and source.get("title"):
        return clean_text(source["title"])

    return feed_name


def is_chelsea_relevant(title, summary):
    """
    Require Chelsea to appear in the headline or summary.
    Also require football-related context.
    """

    title_lower = title.lower()
    combined = (title + " " + summary).lower()

    has_chelsea_in_title = any(
        term in title_lower for term in CHELSEA_TERMS
    )

    has_chelsea_in_content = any(
        term in combined for term in CHELSEA_TERMS
    )

    has_football_context = any(
        term in combined for term in FOOTBALL_TERMS
    )

    if not has_chelsea_in_content:
        return False

    if not has_football_context:
        return False

    # Chelsea must appear in the headline or in a meaningful summary.
    if not has_chelsea_in_title and len(summary) < 40:
        return False

    return True


def contains_low_value_term(title):
    """Identify headlines that are usually not important news."""

    title_lower = title.lower()

    return any(
        term in title_lower for term in LOW_VALUE_TERMS
    )


def calculate_relevance_score(title, summary, source):
    """Score stories according to their Chelsea relevance and importance."""

    title_lower = title.lower()
    combined = (title + " " + summary).lower()

    score = 0

    if "chelsea fc" in title_lower:
        score += 5

    elif "chelsea" in title_lower:
        score += 4

    if "stamford bridge" in title_lower:
        score += 3

    if "chelsea" in summary.lower():
        score += 2

    for term in IMPORTANT_TERMS:
        if term in combined:
            score += 2

    if source.lower() == "bbc sport":
        score += 2

    elif source.lower() == "sky sports":
        score += 2

    elif "google news" in source.lower():
        score += 1

    if contains_low_value_term(title):
        score -= 4

    return max(score, 0)


def title_similarity(title_one, title_two):
    """Compare two headlines for possible duplicates."""

    first = re.sub(
        r"[^a-z0-9 ]",
        "",
        title_one.lower()
    )

    second = re.sub(
        r"[^a-z0-9 ]",
        "",
        title_two.lower()
    )

    similarity = SequenceMatcher(
        None,
        first,
        second
    ).ratio()

    words_one = set(first.split())
    words_two = set(second.split())

    if not words_one or not words_two:
        return similarity

    overlap = len(words_one & words_two) / min(
        len(words_one),
        len(words_two)
    )

    return max(similarity, overlap)


def create_story_id(title, url):
    """Generate a consistent story identifier."""

    value = normalize_url(url) or title.lower()

    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()[:16]


def main():

    checked_at = datetime.now(timezone.utc)

    cutoff_date = checked_at - timedelta(
        days=MAX_AGE_DAYS
    )

    stories = []
    rejected_old = 0
    rejected_irrelevant = 0
    rejected_empty = 0
    feed_errors = []

    print("====================================")
    print("CHELSEAFEEDS COLLECTOR VERSION", VERSION)
    print("====================================")

    print("Starting Chelsea news collection...")
    print("Maximum article age:", MAX_AGE_DAYS, "days")

    for feed_name, feed_url in RSS_FEEDS.items():

        print("\nChecking:", feed_name)

        try:

            feed = feedparser.parse(feed_url)

            if feed.bozo and not feed.entries:
                raise Exception("Feed could not be parsed")

            print("Articles received:", len(feed.entries))

            for entry in feed.entries:

                title = clean_text(
                    entry.get("title", "")
                )

                summary = clean_text(
                    entry.get("summary", "")
                    or entry.get("description", "")
                )

                url = entry.get("link", "").strip()

                if not title or not url:
                    rejected_empty += 1
                    continue

                published = parse_date(entry)

                # Exclude articles older than seven days.
                if published and published < cutoff_date:
                    rejected_old += 1
                    continue

                # Exclude irrelevant football stories.
                if not is_chelsea_relevant(title, summary):
                    rejected_irrelevant += 1
                    continue

                source = get_source_name(
                    feed_name,
                    entry
                )

                score = calculate_relevance_score(
                    title,
                    summary,
                    source
                )

                story = {
                    "id": create_story_id(title, url),
                    "title": title,
                    "summary": summary,
                    "url": url,
                    "normalized_url": normalize_url(url),
                    "source": source,
                    "feed": feed_name,
                    "published": (
                        published.isoformat()
                        if published else None
                    ),
                    "relevance_score": score,
                    "possible_duplicate": False,
                    "duplicate_of": None,
                    "status": "candidate",
                }

                stories.append(story)

        except Exception as error:

            message = str(error)

            print("Feed error:", feed_name, message)

            feed_errors.append({
                "feed": feed_name,
                "error": message
            })

    # Sort highest relevance first.
    stories.sort(
        key=lambda item: item["relevance_score"],
        reverse=True
    )

    # Detect duplicate URLs and similar headlines.
    unique_stories = []
    seen_urls = set()

    for story in stories:

        normalized_url = story["normalized_url"]

        if normalized_url and normalized_url in seen_urls:
            continue

        duplicate_found = False

        for existing in unique_stories:

            similarity = title_similarity(
                story["title"],
                existing["title"]
            )

            if similarity >= 0.78:

                story["possible_duplicate"] = True

                story["duplicate_of"] = existing["id"]

                duplicate_found = True

                break

        # Keep possible duplicates in the output for inspection.
        # They will not be treated as unique publishing candidates.
        if normalized_url:
            seen_urls.add(normalized_url)

        unique_stories.append(story)

    # Prepare output.
    output = {
        "collector_version": VERSION,
        "checked_at": checked_at.isoformat(),
        "maximum_age_days": MAX_AGE_DAYS,
        "total_stories": len(unique_stories),
        "unique_stories": sum(
            1 for story in unique_stories
            if not story["possible_duplicate"]
        ),
        "possible_duplicates": sum(
            1 for story in unique_stories
            if story["possible_duplicate"]
        ),
        "rejected_old": rejected_old,
        "rejected_irrelevant": rejected_irrelevant,
        "rejected_empty": rejected_empty,
        "feed_errors": feed_errors,
        "stories": unique_stories[:MAX_STORIES],
    }

    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            output,
            file,
            indent=2,
            ensure_ascii=False
        )

    print("\n====================================")
    print("COLLECTION COMPLETED")
    print("====================================")

    print("Collector version:", VERSION)
    print("Stories collected:", output["total_stories"])
    print("Unique stories:", output["unique_stories"])
    print("Possible duplicates:", output["possible_duplicates"])
    print("Rejected old stories:", rejected_old)
    print("Rejected irrelevant stories:", rejected_irrelevant)
    print("Feed errors:", len(feed_errors))
    print("Output file:", OUTPUT_FILE)


if __name__ == "__main__":
    main()
