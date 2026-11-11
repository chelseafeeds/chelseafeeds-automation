import feedparser
import requests
import json
import re
import hashlib
import time
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from difflib import SequenceMatcher
from collections import defaultdict

VERSION = "3.2"
MAX_AGE_DAYS = 7
MAX_STORIES = 50
MAX_HISTORY = 5000
HISTORY_DAYS = 60
REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ChelseaFeedsNewsCollector/3.2; "
        "+https://www.chelseafeeds.com)"
    )
}

NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(days=MAX_AGE_DAYS)

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
            "q=Chelsea+FC+when:7d&hl=en-GB&gl=GB&ceid=GB:en"
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
        "url": "https://www.football365.com/feed"
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

OFFICIAL_DOMAINS = {
    "chelseafc.com",
    "premierleague.com",
    "uefa.com",
    "thefa.com"
}

CHELSEA_TERMS = [
    "chelsea", "the blues", "stamford bridge",
    "chelsea fc", "chelsea women", "chelsea academy",
    "chelsea u21", "chelsea u18"
]

IRRELEVANT_TERMS = [
    "chelsea handler", "chelsea boots", "chelsea flower",
    "chelsea neighbourhood", "chelsea manning",
    "chelsea hotel", "chelsea clinton"
]

RUMOUR_TERMS = [
    "rumour", "rumor", "report", "reportedly",
    "linked with", "interested in", "could sign",
    "transfer target", "transfer talks",
    "bid rejected", "bid submitted", "set to sign",
    "deal close", "pursuit", "considering signing"
]

OPINION_TERMS = [
    "opinion", "column", "explained", "why",
    "should", "could have", "player ratings",
    "power rankings", "analysis", "predicted",
    "prediction", "tactical"
]

IMPORTANT_TERMS = [
    "official", "confirmed", "signs", "signed",
    "appointed", "sacked", "manager",
    "injury", "injured", "returns",
    "transfer", "contract", "suspension",
    "premier league", "champions league",
    "fa cup", "carabao cup", "final",
    "semi-final", "quarter-final",
    "match result", "wins", "defeat",
    "draw", "fixture", "lineup", "squad"
]

session = requests.Session()
session.headers.update(HEADERS)


def clean_text(value):
    if not value:
        return ""

    value = BeautifulSoup(str(value), "html.parser").get_text(" ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def parse_date(entry):
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)

        if parsed:
            try:
                from calendar import timegm
                return datetime.fromtimestamp(
                    timegm(parsed), timezone.utc
                )
            except Exception:
                pass

    for key in ("published", "updated", "date"):
        value = entry.get(key)

        if value:
            try:
                from email.utils import parsedate_to_datetime
                result = parsedate_to_datetime(value)

                if result.tzinfo is None:
                    result = result.replace(tzinfo=timezone.utc)

                return result.astimezone(timezone.utc)
            except Exception:
                pass

    return None


def normalize_url(url):
    if not url:
        return ""

    try:
        parsed = urlparse(url.strip())

        if not parsed.scheme or not parsed.netloc:
            return url.strip()

        hostname = parsed.netloc.lower()
        path = re.sub(r"/+$", "", parsed.path)

        ignored = {
            "utm_source", "utm_medium", "utm_campaign",
            "utm_term", "utm_content", "fbclid",
            "gclid", "mc_cid", "mc_eid"
        }

        query = [
            (k, v)
            for k, v in parse_qsl(parsed.query)
            if k.lower() not in ignored
        ]

        return urlunparse((
            parsed.scheme.lower(),
            hostname,
            path,
            "",
            urlencode(query),
            ""
        ))

    except Exception:
        return url


def domain_of(url):
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def is_chelsea_relevant(title, summary):
    text = f"{title} {summary}".lower()

    if any(term in text for term in IRRELEVANT_TERMS):
        return False

    return any(term in text for term in CHELSEA_TERMS)


def resolve_article_url(url):
    """
    Attempt to follow Google News and other redirect links.
    Keep the original URL if resolution fails.
    """

    if not url:
        return url

    if "news.google.com" not in url and "c.newsnow.com" not in url:
        return url

    try:
        response = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True
        )

        final_url = response.url

        if final_url and "news.google.com" not in domain_of(final_url):
            if "newsnow.com" not in domain_of(final_url):
                return final_url

        soup = BeautifulSoup(response.text, "html.parser")

        canonical = soup.find(
            "link",
            attrs={"rel": lambda value: value and "canonical" in value}
        )

        if canonical and canonical.get("href"):
            candidate = canonical["href"]

            if "news.google.com" not in domain_of(candidate):
                return candidate

    except Exception:
        pass

    return url


def classify_story(title, summary, url):
    text = f"{title} {summary}".lower()
    domain = domain_of(url)

    if any(term in text for term in OPINION_TERMS):
        story_type = "opinion"
    elif any(term in text for term in RUMOUR_TERMS):
        story_type = "transfer_rumour"
    else:
        story_type = "news"

    if domain in OFFICIAL_DOMAINS:
        verification = "official_source"
    else:
        verification = "not_verified"

    return story_type, verification


def importance_score(title, summary, source, story_type):
    text = f"{title} {summary}".lower()
    score = 0

    if "chelsea" in text:
        score += 3

    if "chelsea fc" in text:
        score += 2

    for term in IMPORTANT_TERMS:
        if term in text:
            score += 2

    if source in ("BBC Sport", "Sky Sports", "The Guardian - Chelsea"):
        score += 2

    if story_type == "opinion":
        score -= 2

    if story_type == "transfer_rumour":
        score -= 1

    return max(0, min(score, 30))


def story_id(title, url):
    identity = normalize_url(url) or title.lower().strip()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def make_story(title, summary, url, source, published=None,
               discovery_method="rss", publisher=None):

    title = clean_text(title)
    summary = clean_text(summary)
    original_url = url

    if not title or not url:
        return None

    if not is_chelsea_relevant(title, summary):
        return None

    resolved_url = resolve_article_url(url)
    normalized = normalize_url(resolved_url)

    story_type, verification = classify_story(
        title, summary, resolved_url
    )

    date_unverified = published is None

    if published is None:
        published_text = None
    else:
        published_text = published.isoformat()

    return {
        "id": story_id(title, normalized),
        "title": title,
        "summary": summary[:1500],
        "url": resolved_url,
        "original_url": original_url,
        "normalized_url": normalized,
        "source": source,
        "publisher": publisher or source,
        "published": published_text,
        "date_unverified": date_unverified,
        "story_type": story_type,
        "verification_status": verification,
        "corroborating_sources": [],
        "corroboration_count": 1,
        "importance_score": importance_score(
            title, summary, source, story_type
        ),
        "discovery_method": discovery_method
    }


def collect_rss(feed_config):
    results = []
    report = {
        "source": feed_config["name"],
        "type": "RSS",
        "status": "failed",
        "stories_found": 0,
        "error": None
    }

    try:
        response = session.get(
            feed_config["url"],
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()

        feed = feedparser.parse(response.content)

        if not feed.entries:
            report["error"] = "Feed returned no entries"
            return results, report

        for entry in feed.entries:
            published = parse_date(entry)

            if published and published < CUTOFF:
                continue

            title = clean_text(entry.get("title", ""))
            summary = clean_text(
                entry.get("summary", entry.get("description", ""))
            )
            url = entry.get("link", "")

            story = make_story(
                title,
                summary,
                url,
                feed_config["name"],
                published,
                "rss"
            )

            if story:
                results.append(story)

            time.sleep(0.15)

        report["stories_found"] = len(results)
        report["status"] = "success" if results else "empty"

    except Exception as error:
        report["error"] = str(error)[:300]

    return results, report


def collect_newsnow(feed_config):
    results = []

    report = {
        "source": feed_config["name"],
        "type": "Webpage discovery",
        "status": "failed",
        "stories_found": 0,
        "error": None
    }

    try:
        response = session.get(
            feed_config["url"],
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        seen_links = set()

        for anchor in soup.find_all("a", href=True):
            title = clean_text(anchor.get_text(" ", strip=True))
            href = anchor["href"]

            if not title or len(title) < 20:
                continue

            if href.startswith("/"):
                continue

            if href.startswith("//"):
                href = "https:" + href

            if not href.startswith("http"):
                continue

            if "newsnow.com" not in domain_of(href):
                continue

            if href in seen_links:
                continue

            seen_links.add(href)

            parent_text = clean_text(
                anchor.parent.get_text(" ", strip=True)
            )

            publisher = None

            # NewsNow displays the publisher beside the headline.
            # Try the nearby text without treating it as verified.
            if parent_text and parent_text != title:
                publisher = parent_text.replace(title, "").strip()
                publisher = publisher[:100] or None

            story = make_story(
                title,
                parent_text,
                href,
                feed_config["name"],
                None,
                "newsnow",
                publisher
            )

            if story:
                results.append(story)

        report["stories_found"] = len(results)

        if results:
            report["status"] = "success"
        else:
            report["status"] = "empty"
            report["error"] = (
                "Page loaded, but no matching NewsNow links were extracted"
            )

    except Exception as error:
        report["error"] = str(error)[:300]

    return results, report


def similar_titles(first, second):
    a = re.sub(r"[^a-z0-9 ]", "", first.lower())
    b = re.sub(r"[^a-z0-9 ]", "", second.lower())

    return SequenceMatcher(None, a, b).ratio() >= 0.84


def merge_duplicates(stories):
    """
    Merge matching stories while preserving publisher evidence.
    """

    merged = []

    for story in stories:
        match = None

        for existing in merged:
            same_url = (
                story["normalized_url"]
                and story["normalized_url"] == existing["normalized_url"]
            )

            same_title = similar_titles(
                story["title"], existing["title"]
            )

            if same_url or same_title:
                match = existing
                break

        if match:
            publishers = set(match["corroborating_sources"])
            publishers.add(match["publisher"])
            publishers.add(story["publisher"])

            match["corroborating_sources"] = sorted(
                p for p in publishers if p
            )
            match["corroboration_count"] = len(
                match["corroborating_sources"]
            )

            # Prefer a direct publisher URL over an aggregator URL.
            old_domain = domain_of(match["url"])
            new_domain = domain_of(story["url"])

            if (
                old_domain in ("news.google.com", "c.newsnow.com")
                and new_domain not in ("news.google.com", "c.newsnow.com")
            ):
                match["url"] = story["url"]
                match["normalized_url"] = story["normalized_url"]

            if story["importance_score"] > match["importance_score"]:
                match["importance_score"] = story["importance_score"]

            if match["verification_status"] != "official_source":
                if story["verification_status"] == "official_source":
                    match["verification_status"] = "official_source"

        else:
            story["corroborating_sources"] = [story["publisher"]]
            merged.append(story)

    return merged


def load_history():
    try:
        with open("seen_stories.json", "r", encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, dict):
            return data

    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return {}


def save_history(history):
    cutoff = NOW - timedelta(days=HISTORY_DAYS)

    cleaned = {}

    for key, value in history.items():
        try:
            seen_at = datetime.fromisoformat(value)

            if seen_at.tzinfo is None:
                seen_at = seen_at.replace(tzinfo=timezone.utc)

            if seen_at >= cutoff:
                cleaned[key] = value

        except Exception:
            continue

    # Keep the most recent records if the file grows too large.
    cleaned = dict(
        sorted(
            cleaned.items(),
            key=lambda item: item[1],
            reverse=True
        )[:MAX_HISTORY]
    )

    with open("seen_stories.json", "w", encoding="utf-8") as file:
        json.dump(cleaned, file, indent=2)


def main():
    print(f"Starting ChelseaFeeds Collector v{VERSION}")

    all_stories = []
    source_reports = []

    for feed_config in FEEDS:
        print(f"Checking: {feed_config['name']}")

        if feed_config["type"] == "rss":
            stories, report = collect_rss(feed_config)
        else:
            stories, report = collect_newsnow(feed_config)

        all_stories.extend(stories)
        source_reports.append(report)

        print(
            f"  {report['status']}: "
            f"{report['stories_found']} stories"
        )

        time.sleep(0.5)

    unique_stories = merge_duplicates(all_stories)

    history = load_history()
    new_count = 0

    for story in unique_stories:
        identifier = story["id"]

        story["previously_seen"] = identifier in history

        if identifier in history:
            story["first_seen_or_last_seen"] = history[identifier]
        else:
            story["first_seen_or_last_seen"] = NOW.isoformat()
            new_count += 1

        history[identifier] = NOW.isoformat()

    save_history(history)

    # Highest importance first; newest publication date as a secondary sort.
    unique_stories.sort(
        key=lambda item: (
            item["importance_score"],
            item["published"] or ""
        ),
        reverse=True
    )

    selected = unique_stories[:MAX_STORIES]

    report = {
        "collector_version": VERSION,
        "checked_at": NOW.isoformat(),
        "maximum_age_days": MAX_AGE_DAYS,
        "configured_sources": len(FEEDS),
        "successful_sources": sum(
            1 for item in source_reports
            if item["status"] == "success"
        ),
        "failed_sources": sum(
            1 for item in source_reports
            if item["status"] == "failed"
        ),
        "empty_sources": sum(
            1 for item in source_reports
            if item["status"] == "empty"
        ),
        "total_collected": len(all_stories),
        "unique_stories": len(unique_stories),
        "new_stories": new_count,
        "previously_seen_stories": len(unique_stories) - new_count,
        "selected_for_review": len(selected),
        "source_report": source_reports,
        "publishing_note": (
            "Discovery candidates only. Official-source labels indicate "
            "the publisher domain, not independent fact-checking. "
            "Multiple reports are not automatically independent "
            "corroboration. No articles have been published."
        )
    }

    output = {
        "report": report,
        "stories": selected
    }

    with open(
        "news_candidates.json", "w", encoding="utf-8"
    ) as file:
        json.dump(output, file, indent=2, ensure_ascii=False)

    with open(
        "collection_report.json", "w", encoding="utf-8"
    ) as file:
        json.dump(report, file, indent=2, ensure_ascii=False)

    print("\nCollection completed.")
    print(f"Total collected: {len(all_stories)}")
    print(f"Unique stories: {len(unique_stories)}")
    print(f"New stories: {new_count}")
    print(f"Selected: {len(selected)}")
    print("Files created:")
    print("- news_candidates.json")
    print("- collection_report.json")
    print("- seen_stories.json")


if __name__ == "__main__":
    main()
