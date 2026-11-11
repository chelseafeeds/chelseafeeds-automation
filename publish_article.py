#!/usr/bin/env python3
"""Create or update one reviewed ChelseaFeeds Blogger draft; optionally publish and announce."""
import html
import json
import os
import re
import sys
from urllib.parse import quote, urlparse

import requests

API = "https://www.googleapis.com/blogger/v3"

def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required GitHub Actions secret/variable: {name}")
    return value

def api_headers(token):
    return {"Authorization": f"Bearer {token}"}

def find_posts(session, blog_id, token, status):
    endpoint = f"{API}/blogs/{quote(blog_id, safe='')}/posts"
    response = session.get(endpoint, params={
        "status": status, "maxResults": 500, "fetchBodies": "false"
    }, headers=api_headers(token), timeout=45)
    response.raise_for_status()
    return response.json().get("items", [])

def attach_featured_image(article_html, image_url, alt_text):
    parsed = urlparse(image_url)
    if (parsed.scheme != "https" or parsed.netloc != "raw.githubusercontent.com"
            or not parsed.path.startswith("/chelseafeeds/chelseafeeds-automation/")):
        raise RuntimeError("Featured image URL is not from the verified ChelseaFeeds public image repository.")
    safe_url = html.escape(image_url, quote=True)
    safe_alt = html.escape(alt_text[:250], quote=True)
    if image_url in article_html:
        return article_html
    image_block = (
        '<div class="separator" style="clear: both; text-align: center;">'
        f'<a href="{safe_url}" style="margin-left: 1em; margin-right: 1em;">'
        f'<img src="{safe_url}" alt="{safe_alt}" width="1200" height="675" '
        'style="display: block; max-width: 100%; height: auto; margin: 0 auto;" />'
        '</a></div>'
    )
    return image_block + article_html

def main():
    artifact = os.getenv("BLOGGER_READY_FILE", "input/blogger_ready_posts.json")
    if not os.path.isfile(artifact):
        raise RuntimeError(f"Article artifact not found: {artifact}")
    with open(artifact, encoding="utf-8") as file:
        payload = json.load(file)
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
    article_html = str(post.get("content_html", "")).strip()
    description = str(post.get("search_description", "")).strip()
    if not title or not article_html or not (100 <= len(description) <= 180):
        raise RuntimeError("Article is missing required Blogger title, HTML, or SEO description.")
    if re.search(r"<\s*(script|iframe|form|object|embed)\b", article_html, re.I):
        raise RuntimeError("Unsafe HTML detected; refusing to send article to Blogger.")

    image_url = required("FEATURED_IMAGE_URL")
    image_alt = required("FEATURED_IMAGE_ALT")
    content = attach_featured_image(article_html, image_url, image_alt)

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

    session = requests.Session()
    headers = api_headers(access_token)
    live = os.getenv("BLOGGER_PUBLISH_LIVE", "false").lower() == "true"
    body = {
        "kind": "blogger#post",
        "title": title,
        "content": content,
        "labels": post.get("labels", []) if isinstance(post.get("labels"), list) else [],
        "customMetaData": description
    }
    base = f"{API}/blogs/{quote(blog_id, safe='')}/posts"

    # Make repeated draft tests safe: update the matching existing draft instead of duplicating it.
    drafts = find_posts(session, blog_id, access_token, "draft")
    matching_drafts = [item for item in drafts if item.get("title") == title and item.get("id")]
    if len(matching_drafts) > 1:
        raise RuntimeError("Multiple Blogger drafts already have this exact title; refusing an ambiguous update.")
    live_posts = find_posts(session, blog_id, access_token, "live")
    matching_live = [item for item in live_posts if item.get("title") == title and item.get("id")]
    if matching_live:
        raise RuntimeError("A public Blogger post already has this exact title; refusing to create a duplicate.")

    if matching_drafts:
        post_id = str(matching_drafts[0]["id"])
        response = session.put(f"{base}/{quote(post_id, safe='')}", params={
            "fetchImages": "true", "fetchBody": "true"
        }, json=body, headers=headers, timeout=45)
        response.raise_for_status()
        result = response.json()
        blogger_action = "updated_existing_draft"
        if live:
            publish_response = session.post(
                f"{base}/{quote(post_id, safe='')}/publish",
                headers=headers, timeout=45
            )
            publish_response.raise_for_status()
            result = publish_response.json()
            blogger_action = "published_existing_draft"
    else:
        response = session.post(base, params={
            "isDraft": "false" if live else "true",
            "fetchImages": "true", "fetchBody": "true"
        }, json=body, headers=headers, timeout=45)
        response.raise_for_status()
        result = response.json()
        blogger_action = "published_new_post" if live else "created_new_draft"

    post_url = result.get("url", "")
    post_id = result.get("id", post_id if "post_id" in locals() else "")
    if not post_id:
        raise RuntimeError("Blogger API returned no post ID; verify the post in Blogger.")
    images = result.get("images", []) or []
    detected = any(isinstance(item, dict) and item.get("url") == image_url for item in images)
    response_content = result.get("content", "") or ""
    image_status = "blogger_api_detected_image" if detected else (
        "image_present_in_saved_html" if image_url in response_content else "image_embedded_in_request"
    )
    print(json.dumps({
        "blogger_status": "published" if live else "saved_as_draft",
        "blogger_action": blogger_action,
        "post_id": str(post_id),
        "title": title,
        "post_url": post_url,
        "featured_image_status": image_status,
        "featured_image_url": image_url
    }, ensure_ascii=False))

    # X is strictly downstream of a publicly published Blogger post.
    if live and os.getenv("ENABLE_X_POSTING", "false").lower() == "true":
        from requests_oauthlib import OAuth1
        auth = OAuth1(required("X_API_KEY"), required("X_API_SECRET"),
                      required("X_ACCESS_TOKEN"), required("X_ACCESS_TOKEN_SECRET"))
        announcement = f"{title}\n\nRead more: {post_url}"
        if len(announcement) > 280:
            announcement = title[:max(40, 275-len(post_url))].rstrip() + "…\n\n" + post_url
        x_response = requests.post("https://api.x.com/2/tweets",
            json={"text": announcement}, auth=auth, timeout=30)
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
