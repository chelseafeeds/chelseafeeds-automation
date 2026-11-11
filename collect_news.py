import feedparser
import json
import re
import html
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

FEEDS = {
    "BBC Sport": "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "Sky Sports": "https://www.skysports.com/rss/11095",
    "Google News": (
        "https://news.google.com/rss/search?"
        "q=Chelsea+FC&hl=en-GB&gl=GB&ceid=GB:en"
    ),
}

CHELSEA_TERMS = [
    "chelsea",
    "stamford bridge",
    "the blues",
    "chelsea fc",
]

def clean_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def normalize_url(url):
    parts = urlsplit(url)
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path.rstrip("/"),
        "",
        ""
    ))

def is_chelsea_story(title, summary):
    text = f"{title} {summary}".lower()
    return any(term in text for term in CHELSEA_TERMS)

def main():
    stories = []
    seen = set()

    for source, feed_url in FEEDS.items():
        print(f"Checking {source}...")

        try:
            feed = feedparser.parse(feed_url)

            if feed.bozo and not feed.entries:
                print(f"Feed unavailable: {source}")
                continue

            for entry in feed.entries:
                title = clean_text(entry.get("title"))
                summary = clean_text(entry.get("summary"))
                link = entry.get("link", "")

                if not title or not link:
                    continue

                if not is_chelsea_story(title, summary):
                    continue

                normalized = normalize_url(link)
                key = normalized or title.lower()

                if key in seen:
                    continue

                seen.add(key)

                stories.append({
                    "source": source,
                    "title": title,
                    "summary": summary,
                    "url": link,
                    "published": entry.get("published", ""),
                })

        except Exception as error:
            print(f"Error checking {source}: {error}")

    output = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "total_stories": len(stories),
        "stories": stories,
    }

    with open("news_candidates.json", "w", encoding="utf-8") as file:
        json.dump(output, file, indent=2, ensure_ascii=False)

    print(f"\nCollection completed.")
    print(f"Chelsea stories found: {len(stories)}")

    for story in stories:
        print(f"\n[{story['source']}] {story['title']}")
        print(story["url"])

if __name__ == "__main__":
    main()
    
