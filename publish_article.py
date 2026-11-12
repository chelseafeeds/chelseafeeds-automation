#!/usr/bin/env python3
"""Create or update a ChelseaFeeds Blogger post and schedule it when auto-scheduling is enabled."""
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
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

    if source_type == "licensed_original_source_photo":
        source = urlparse(credit_url)
        license_link = urlparse(license_url) if license_url else None
        if source.scheme != "https" or not source.netloc:
            raise RuntimeError("Licensed source photo is missing a valid HTTPS attribution page.")
        accepted = {"CC BY 2.0", "CC BY 3.0", "CC BY 4.0", "CC0 1.0", "PUBLIC DOMAIN MARK 1.0"}
        if license_name.upper() not in accepted:
            raise RuntimeError("Original-source image is missing an exact accepted image-specific licence name.")
        if license_link and (license_link.scheme != "https" or license_link.netloc not in {"creativecommons.org", "www.creativecommons.org"}):
            raise RuntimeError("Commons photo licence URL is not an approved Creative Commons URL.")
        if not license_link:
            raise RuntimeError("Original-source image is missing its licence URL.")
        license_caption = (
            f' · <a href="{html.escape(license_url, quote=True)}" rel="nofollow noopener">{html.escape(license_name)}</a>'
            if license_url else f' · {html.escape(license_name)}'
        )
        caption = (
            '<figcaption style="font-size: small; color: #666; text-align: center;">'
            f'{html.escape(credit_text)} · '
            f'<a href="{html.escape(credit_url, quote=True)}" rel="nofollow noopener">Image source</a>'
            f'{license_caption}</figcaption>'
        )
    elif source_type == "ai_generated_illustration":
        caption = '<figcaption style="font-size: small; color: #666; text-align: center;">AI-generated editorial illustration; not a documentary match photograph.</figcaption>'
    else:
        raise RuntimeError("Refusing to publish an article without a verified original-source featured image.")

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

    # User authorized automatic publication; retain article quality/evidence processing,
    # but do not block scheduling solely because the editorial metadata requests review.

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

    # Publishing is permitted only through an explicit, bounded future schedule.
    # A zero-minute setting preserves draft-only behaviour.
    schedule_minutes = int(os.getenv("BLOGGER_AUTO_SCHEDULE_MINUTES", "0"))
    if schedule_minutes < 0 or schedule_minutes > 1440:
        raise RuntimeError("BLOGGER_AUTO_SCHEDULE_MINUTES must be between 0 and 1440.")

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
    scheduled_posts = find_posts(session, blog_id, access_token, "scheduled")
    matching_scheduled = [item for item in scheduled_posts if item.get("title") == title and item.get("id")]
    if len(matching_scheduled) > 1:
        raise RuntimeError("Multiple scheduled Blogger posts already have this exact title; refusing an ambiguous update.")

    if matching_scheduled:
        result = matching_scheduled[0]
        post_id = str(result["id"])
        blogger_action = "already_scheduled"
    elif matching_drafts:
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

    scheduled_for = None
    blogger_status = "saved_as_draft"
    if schedule_minutes and blogger_action != "already_scheduled":
        scheduled_for = (datetime.now(timezone.utc) + timedelta(minutes=schedule_minutes)).isoformat(timespec="seconds").replace("+00:00", "Z")
        publish_endpoint = f"{base}/{quote(str(post_id), safe='')}/publish"
        publish_response = session.post(publish_endpoint, params={"publishDate": scheduled_for}, headers=headers, timeout=45)
        publish_response.raise_for_status()
        result = publish_response.json()
        blogger_status = "scheduled"
        post_url = result.get("url", post_url)
    elif blogger_action == "already_scheduled":
        blogger_status = "already_scheduled"
        scheduled_for = result.get("published")

    images = result.get("images", []) or []
    detected = any(isinstance(item, dict) and item.get("url") == image_url for item in images)
    response_content = result.get("content", "") or ""
    image_status = "blogger_api_detected_image" if detected else (
        "image_present_in_saved_html" if image_url in response_content else "image_embedded_in_request"
    )
    print(json.dumps({
        "blogger_status": blogger_status,
        "blogger_action": blogger_action,
        "post_id": str(post_id),
        "title": title,
        "post_url": post_url,
        "search_description_characters": len(description),
        "scheduled_for_utc": scheduled_for,
        "schedule_delay_minutes": schedule_minutes,
        "featured_image_status": image_status,
        "featured_image_source_type": os.getenv("FEATURED_IMAGE_SOURCE_TYPE", "branded_graphic_fallback"),
        "featured_image_url": image_url,
        "public_publishing_enabled": bool(schedule_minutes)
    }, ensure_ascii=False))
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"post_id={post_id}\\n")
            output.write(f"post_url={post_url}\\n")
            output.write(f"title={title.replace(chr(10), ' ').replace(chr(13), ' ')}\\n")
            output.write(f"search_description={description.replace(chr(10), ' ').replace(chr(13), ' ')}\\n")
    print("Blogger publishing completed; X posting is handled by the live-status-gated workflow step.")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
