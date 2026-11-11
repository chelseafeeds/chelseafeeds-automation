#!/usr/bin/env python3
"""Create or update one reviewed ChelseaFeeds Blogger draft; public publishing is hard-disabled."""
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
    source_type = os.getenv("FEATURED_IMAGE_SOURCE_TYPE", "branded_graphic_fallback").strip()
    credit_text = os.getenv("FEATURED_IMAGE_CREDIT_TEXT", "ChelseaFeeds editorial graphic").strip()
    credit_url = os.getenv("FEATURED_IMAGE_CREDIT_URL", "").strip()
    license_name = os.getenv("FEATURED_IMAGE_LICENSE_NAME", "").strip()
    license_url = os.getenv("FEATURED_IMAGE_LICENSE_URL", "").strip()

    if source_type == "licensed_commons_photo":
        source = urlparse(credit_url)
        license_link = urlparse(license_url) if license_url else None
        if source.scheme != "https" or source.netloc != "commons.wikimedia.org" or not source.path.startswith("/wiki/"):
            raise RuntimeError("Commons photo is missing a valid Wikimedia Commons attribution page.")
        if not license_name or not license_url:
            raise RuntimeError("Commons photo is missing its licence name or licence URL.")
        if not license_link or license_link.scheme != "https" or license_link.netloc not in {"creativecommons.org", "creativecommons.org"}:
            if "public domain" not in license_name.lower() and "cc0" not in license_name.lower():
                raise RuntimeError("Commons photo licence URL is not an approved Creative Commons URL.")
        caption = (
            '<figcaption style="font-size: small; color: #666; text-align: center;">'
            f'{html.escape(credit_text)} · '
            f'<a href="{html.escape(credit_url, quote=True)}" rel="nofollow noopener">Image source</a> · '
            f'<a href="{html.escape(license_url, quote=True)}" rel="nofollow noopener">{html.escape(license_name)}</a>'
            '</figcaption>'
        )
    elif source_type == "ai_generated_illustration":
        caption = '<figcaption style="font-size: small; color: #666; text-align: center;">AI-generated editorial illustration; not a documentary match photograph.</figcaption>'
    else:
        caption = '<figcaption style="font-size: small; color: #666; text-align: center;">ChelseaFeeds editorial graphic.</figcaption>'

    if image_url in article_html:
        return article_html
    image_block = (
        '<figure class="separator" style="clear: both; text-align: center; margin: 0 auto 1.2em;">'
        f'<img src="{safe_url}" alt="{safe_alt}" width="1200" height="675" '
        'style="display: block; max-width: 100%; height: auto; margin: 0 auto;" />'
        f'{caption}</figure>'
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

    # Explicit safety lock: this integration only creates/updates Blogger drafts.
    if os.getenv("BLOGGER_PUBLISH_LIVE", "false").lower() == "true":
        raise RuntimeError("Public publishing is disabled in this testing version.")
    live = False

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
    body = {
        "kind": "blogger#post",
        "title": title,
        "content": content,
        "labels": post.get("labels", []) if isinstance(post.get("labels"), list) else [],
        "customMetaData": description
    }
    base = f"{API}/blogs/{quote(blog_id, safe='')}/posts"

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
    else:
        response = session.post(base, params={
            "isDraft": "true", "fetchImages": "true", "fetchBody": "true"
        }, json=body, headers=headers, timeout=45)
        response.raise_for_status()
        result = response.json()
        blogger_action = "created_new_draft"

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
        "blogger_status": "saved_as_draft",
        "blogger_action": blogger_action,
        "post_id": str(post_id),
        "title": title,
        "post_url": post_url,
        "featured_image_status": image_status,
        "featured_image_source_type": os.getenv("FEATURED_IMAGE_SOURCE_TYPE", "branded_graphic_fallback"),
        "featured_image_url": image_url,
        "public_publishing_enabled": False
    }, ensure_ascii=False))
    print("X announcement skipped (draft-only testing mode; X publishing disabled).")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
