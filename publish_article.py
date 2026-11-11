#!/usr/bin/env python3
"""Publish one reviewed ChelseaFeeds article to Blogger; optionally announce a live post on X."""
import json, os, re, sys
from pathlib import Path
from urllib.parse import quote
import requests

API = "https://www.googleapis.com/blogger/v3"

def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required GitHub Actions secret/variable: {name}")
    return value

def main():
    artifact = Path(os.getenv("BLOGGER_READY_FILE", "input/blogger_ready_posts.json"))
    if not artifact.exists():
        raise RuntimeError(f"Article artifact not found: {artifact}")
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    posts = payload.get("posts", []) if isinstance(payload, dict) else payload
    if not isinstance(posts, list):
        raise RuntimeError("blogger_ready_posts.json has an unexpected format.")
    event_id = required("ARTICLE_EVENT_ID")
    matches = [p for p in posts if str(p.get("event_id", "")) == event_id]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one article for event_id={event_id}; found {len(matches)}")
    post = matches[0]
    if post.get("editorial_status") != "draft_requires_editorial_review":
        raise RuntimeError("Article is not marked as requiring editorial review; refusing to publish.")
    if post.get("claims_verified") is not False:
        raise RuntimeError("Unexpected claims_verified value; refusing to publish.")
    if post.get("publishing_enabled") is not False:
        raise RuntimeError("Unexpected publishing_enabled value; refusing to publish.")
    title = str(post.get("title", "")).strip()
    html = str(post.get("content_html", "")).strip()
    description = str(post.get("search_description", "")).strip()
    if not title or not html or not (100 <= len(description) <= 180):
        raise RuntimeError("Article is missing required Blogger title, HTML, or SEO description.")
    if re.search(r"<\s*(script|iframe|form|object|embed)\b", html, re.I):
        raise RuntimeError("Unsafe HTML detected; refusing to send article to Blogger.")

    client_id = required("BLOGGER_CLIENT_ID")
    client_secret = required("BLOGGER_CLIENT_SECRET")
    refresh_token = required("BLOGGER_REFRESH_TOKEN")
    blog_id = required("BLOGGER_BLOG_ID")
    token_response = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token, "grant_type": "refresh_token"
    }, timeout=30)
    token_response.raise_for_status()
    access_token = token_response.json()["access_token"]

    live = os.getenv("BLOGGER_PUBLISH_LIVE", "false").lower() == "true"
    params = {"isDraft": "false" if live else "true"}
    body = {"kind": "blogger#post", "title": title, "content": html,
            "labels": post.get("labels", []), "customMetaData": description}
    endpoint = f"{API}/blogs/{quote(blog_id, safe='')}/posts/"
    response = requests.post(endpoint, params=params, json=body,
        headers={"Authorization": f"Bearer {access_token}"}, timeout=45)
    response.raise_for_status()
    result = response.json()
    post_url = result.get("url", "")
    if not post_url:
        raise RuntimeError("Blogger API returned no post URL; X announcement skipped.")
    print(json.dumps({"blogger_status": "published" if live else "saved_as_draft",
                      "title": title, "post_url": post_url}, ensure_ascii=False))

    # X is strictly downstream of a publicly published Blogger post.
    if live and os.getenv("ENABLE_X_POSTING", "false").lower() == "true":
        from requests_oauthlib import OAuth1
        auth = OAuth1(required("X_API_KEY"), required("X_API_SECRET"),
                      required("X_ACCESS_TOKEN"), required("X_ACCESS_TOKEN_SECRET"))
        text = f"{title}\n\nRead more: {post_url}"
        if len(text) > 280:
            text = title[:max(40, 275-len(post_url))].rstrip() + "…\n\n" + post_url
        x_response = requests.post("https://api.x.com/2/tweets",
            json={"text": text}, auth=auth, timeout=30)
        x_response.raise_for_status()
        print(json.dumps({"x_status": "posted", "tweet_id": x_response.json().get("data", {}).get("id")}))
    else:
        print("X announcement skipped (requires a live Blogger post and ENABLE_X_POSTING=true).")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
