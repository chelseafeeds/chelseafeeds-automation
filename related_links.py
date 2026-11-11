#!/usr/bin/env python3
"""Find relevant, already-published ChelseaFeeds posts and add safe internal links."""
from __future__ import annotations
import json
import logging
import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup

SITE = "https://www.chelseafeeds.com"
FEED_URL = SITE + "/feeds/posts/default?alt=json&max-results=100"
STOPWORDS = {
    "chelsea","chelseafc","fc","the","and","for","with","from","that","this","after",
    "before","about","into","over","under","their","they","them","what","when","where",
    "which","will","would","could","should","latest","news","update","updates","report",
    "reports","analysis","season","football","club","blues","premier","league"
}

def _tokens(value):
    words = re.findall(r"[a-z0-9]{3,}", str(value or "").lower())
    return {w for w in words if w not in STOPWORDS and not w.isdigit()}

def _site_article_url(url):
    parsed = urlparse(url or "")
    return (parsed.scheme == "https" and parsed.netloc.lower() in {"www.chelseafeeds.com","chelseafeeds.com"}
            and re.match(r"^/\d{4}/\d{2}/[^/]+\.html$", parsed.path) is not None)

def fetch_published_articles(session):
    """Read Blogger's public posts feed; it contains live posts, not drafts."""
    try:
        response = session.get(FEED_URL, timeout=12)
        response.raise_for_status()
        payload = response.json()
        entries = payload.get("feed", {}).get("entry", [])
        articles, seen = [], set()
        for entry in entries:
            title = entry.get("title", {}).get("$t", "").strip()
            url = next((x.get("href", "") for x in entry.get("link", [])
                        if x.get("rel") == "alternate" and x.get("type") == "text/html"), "")
            if not title or not _site_article_url(url) or url in seen:
                continue
            seen.add(url)
            summary = entry.get("summary", {}).get("$t", "")
            content = entry.get("content", {}).get("$t", "")
            text = BeautifulSoup(summary + " " + content, "html.parser").get_text(" ", strip=True)
            articles.append({"title": title, "url": url, "description": text[:1800]})
        return articles
    except Exception as exc:
        logging.warning("Could not load public Blogger posts feed; internal links will be skipped: %s",
                        type(exc).__name__)
        return []

def select_related_articles(draft, story, published_articles, limit=3):
    """Conservative lexical matching; never invent a URL or select the current article."""
    query = _tokens(" ".join([
        str(draft.get("title", "")), str(draft.get("focus_keyword", "")),
        str(draft.get("search_description", "")), str(story.get("title", "")),
        str(story.get("best_summary", ""))
    ]))
    if not query:
        return []
    ranked = []
    for article in published_articles:
        if not _site_article_url(article.get("url")):
            continue
        candidate = _tokens(article.get("title", "") + " " + article.get("description", ""))
        shared = query & candidate
        if len(shared) < 2:
            continue
        score = len(shared) / max(1, min(len(query), len(candidate)))
        if score < 0.16:
            continue
        ranked.append((score, len(shared), article))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    result, seen = [], set()
    for score, overlap, article in ranked:
        if article["url"] in seen:
            continue
        seen.add(article["url"])
        result.append({**article, "score": round(score, 3), "shared_terms": overlap})
        if len(result) >= limit:
            break
    return result

def add_related_links(article_html, related):
    """Insert a small related-reading section before Sources; preserve article and source links."""
    soup = BeautifulSoup(str(article_html or ""), "html.parser")
    for heading in list(soup.find_all(["h2", "h3"])):
        if heading.get_text(" ", strip=True).casefold() == "related chelseafeeds coverage":
            node = heading
            while node:
                nxt = node.find_next_sibling()
                node.decompose()
                if nxt and nxt.name in {"h2", "h3"}:
                    break
                node = nxt
    valid = [x for x in related if _site_article_url(x.get("url")) and x.get("title")]
    if not valid:
        return str(soup)
    section = soup.new_tag("section")
    heading = soup.new_tag("h2")
    heading.string = "Related ChelseaFeeds coverage"
    section.append(heading)
    listing = soup.new_tag("ul")
    for item in valid[:3]:
        li = soup.new_tag("li")
        anchor = soup.new_tag("a", href=item["url"])
        anchor.string = item["title"]
        li.append(anchor)
        listing.append(li)
    section.append(listing)
    sources_heading = next((h for h in soup.find_all(["h2", "h3"])
                            if h.get_text(" ", strip=True).casefold() == "sources"), None)
    if sources_heading:
        parent = sources_heading.parent
        if parent and parent.name == "section":
            parent.insert_before(section)
        else:
            sources_heading.insert_before(section)
    else:
        soup.append(section)
    return str(soup)

def self_test():
    articles = [
        {"title":"Chelsea injury update: Reece James fitness latest",
         "url":SITE+"/2026/11/chelsea-reece-james-injury-update.html",
         "description":"Fitness and injury news about Reece James ahead of the next match."},
        {"title":"Arsenal transfer rumours",
         "url":SITE+"/2026/11/arsenal-transfer-rumours.html",
         "description":"Arsenal transfer news and Premier League rumours."},
    ]
    draft = {"title":"Chelsea assess Reece James fitness ahead of Premier League match",
             "focus_keyword":"Reece James injury"}
    story = {"title":"Reece James injury update"}
    selected = select_related_articles(draft, story, articles)
    assert len(selected) == 1 and "reece-james" in selected[0]["url"]
    html = '<h2>Analysis</h2><p>Body</p><h2>Sources</h2><p>Source</p>'
    result = BeautifulSoup(add_related_links(html, selected), "html.parser")
    related = result.find("h2", string="Related ChelseaFeeds coverage")
    sources = result.find("h2", string="Sources")
    assert related and sources and related.find_next("a", href=selected[0]["url"])
    assert related.find_next("h2") == sources
    assert not select_related_articles(draft, story, [
        {"title":"Arsenal transfer rumours","url":"https://evil.example/not-ours.html","description":"Arsenal news"}
    ])
    print("PASS: published-domain allowlist, relevance threshold, safe URLs, related section placement")

if __name__ == "__main__":
    self_test()
