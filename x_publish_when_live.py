#!/usr/bin/env python3
"""Wait until a scheduled Blogger post is actually live, then publish title + thumbnail + tags to X."""
import os, re, sys, time, json
from urllib.parse import quote
import requests
from requests_oauthlib import OAuth1Session

BLOGGER_API = "https://www.googleapis.com/blogger/v3"

def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing GitHub Actions secret: {name}")
    return value

def make_tags(title, labels):
    candidates = list(labels or [])
    # Add a few title-derived related keywords, excluding generic stop words.
    stop = {"chelsea","the","and","for","with","from","after","before","into","their","this","that","over","under","news","latest"}
    words = re.findall(r"[A-Za-z0-9]+", title)
    candidates.extend(w for w in words if len(w) >= 4 and w.lower() not in stop)
    tags, seen = [], set()
    for raw in candidates:
        words = re.findall(r"[A-Za-z0-9]+", str(raw))
        if not words:
            continue
        tag = "".join(x.title() for x in words)
        if not tag or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        tags.append("#" + tag)
        if len(tags) >= 5:
            break
    return tags

def conservative_x_length(text):
    # Conservative Basic-tier guard: Unicode-heavy text is counted more strictly.
    total = 0
    for ch in text:
        code = ord(ch)
        if code > 0xFFFF:
            total += 2
        elif ch in "ilI.,'!|:; ":
            total += 1
        elif code < 128:
            total += 2
        else:
            total += 2
    return total

def main():
    blog_id = required("BLOGGER_BLOG_ID")
    client_id = required("BLOGGER_CLIENT_ID")
    client_secret = required("BLOGGER_CLIENT_SECRET")
    refresh_token = required("BLOGGER_REFRESH_TOKEN")
    post_id = required("BLOGGER_POST_ID")
    title = required("ARTICLE_TITLE")
    image_path = required("ARTICLE_THUMBNAIL_PATH")
    labels = json.loads(os.getenv("ARTICLE_LABELS_JSON", "[]"))
    if not os.path.isfile(image_path):
        raise RuntimeError("Article thumbnail file is missing; refusing to post text without its image.")

    token_response = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token, "grant_type": "refresh_token"
    }, timeout=30)
    token_response.raise_for_status()
    access_token = token_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}
    post_url = f"{BLOGGER_API}/blogs/{quote(blog_id, safe='')}/posts/{quote(post_id, safe='')}"
    deadline = time.time() + 35 * 60
    live_post = None
    while time.time() < deadline:
        response = requests.get(post_url, headers=headers, timeout=30)
        if response.status_code == 404:
            time.sleep(30); continue
        response.raise_for_status()
        item = response.json()
        status = str(item.get("status", "")).upper()
        if status == "LIVE" or (item.get("url") and item.get("published") and not item.get("draft")):
            live_post = item
            break
        print(f"Blogger post is not live yet (status={status or 'pending'}); checking again in 30 seconds.", flush=True)
        time.sleep(30)
    if not live_post:
        raise RuntimeError("Blogger post did not become live within 35 minutes; X publication was withheld.")

    tags = make_tags(title, labels)
    text = title.strip()
    if tags:
        text += "\n\n" + " ".join(tags)
    length = conservative_x_length(text)
    if length > 260:
        while tags and length > 260:
            tags.pop()
            text = title.strip() + (("\n\n" + " ".join(tags)) if tags else "")
            length = conservative_x_length(text)
    if length > 260:
        raise RuntimeError(f"Title alone exceeds conservative Basic X limit ({length}/260); X post withheld.")

    auth = OAuth1Session(
        required("X_API_KEY"), client_secret=required("X_API_SECRET"),
        resource_owner_key=required("X_ACCESS_TOKEN"),
        resource_owner_secret=required("X_ACCESS_TOKEN_SECRET")
    )
    with open(image_path, "rb") as image_file:
        media_response = auth.post(
            "https://upload.twitter.com/1.1/media/upload.json",
            files={"media": image_file},
            data={"media_category": "tweet_image"},
            timeout=90
        )
    media_response.raise_for_status()
    media_id = str(media_response.json()["media_id_string"])
    tweet_response = auth.post(
        "https://api.x.com/2/tweets",
        json={"text": text, "media": {"media_ids": [media_id]}},
        headers={"Content-Type": "application/json"},
        timeout=45
    )
    tweet_response.raise_for_status()
    tweet_id = tweet_response.json().get("data", {}).get("id", "")
    print(json.dumps({
        "x_status": "published",
        "tweet_id": tweet_id,
        "tweet_url": f"https://x.com/i/web/status/{tweet_id}" if tweet_id else "",
        "blogger_post_id": post_id,
        "blogger_url": live_post.get("url", ""),
        "character_guard": length,
        "tags": tags
    }, ensure_ascii=False))

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"X_PUBLISH_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
