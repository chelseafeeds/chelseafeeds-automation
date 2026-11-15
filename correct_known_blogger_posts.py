#!/usr/bin/env python3
"""Correct one verified wording error in the Morgan Rogers scheduled Blogger article."""
import os
import re
import sys
from urllib.parse import quote

import requests

API = "https://www.googleapis.com/blogger/v3"

def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

def main():
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
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    base = f"{API}/blogs/{quote(blog_id, safe='')}/posts"
    target_title = "Morgan Rogers named Chelsea Player of the Month for October 2026"
    matches = []

    for status in ("scheduled", "live", "draft"):
        response = session.get(base, params={
            "status": status.upper(), "maxResults": 100, "fetchBodies": "true"
        }, headers=headers, timeout=30)
        response.raise_for_status()
        payload = response.json()
        found = [item for item in payload.get("items", [])
                 if item.get("title") == target_title and item.get("id")]
        matches.extend((status.upper(), item) for item in found)
        if found:
            break

    unique = {}
    for status, item in matches:
        unique[str(item["id"])] = (status, item)
    if not unique:
        print("CORRECTION_NOOP: matching Blogger post not found; no content changed.")
        return
    if len(unique) != 1:
        raise RuntimeError(f"Found {len(unique)} matching Blogger posts; refusing ambiguous correction.")

    previous_status, item = next(iter(unique.values()))
    post_id = str(item["id"])
    if "content" not in item:
        get_response = session.get(f"{base}/{quote(post_id, safe='')}",
            params={"fetchImages": "true", "fetchBody": "true", "view": "ADMIN"},
            headers=headers, timeout=45)
        get_response.raise_for_status()
        item = get_response.json()

    original_content = str(item.get("content", ""))
    corrected_content, replacements = re.subn(
        r"\bten shots on target\b", "ten shots", original_content, flags=re.I
    )
    if replacements == 0:
        print(f"CORRECTION_NOOP: no incorrect phrase found in post {post_id}; status={previous_status}.")
        return

    body = {
        "kind": "blogger#post",
        "id": post_id,
        "title": item.get("title", target_title),
        "content": corrected_content,
        "labels": item.get("labels", []) if isinstance(item.get("labels"), list) else [],
    }
    update_response = session.put(
        f"{base}/{quote(post_id, safe='')}",
        params={"fetchImages": "true", "fetchBody": "true"},
        json=body, headers=headers, timeout=45
    )
    update_response.raise_for_status()

    verified = update_response.json()
    verified_content = str(verified.get("content", ""))
    current_status = str(verified.get("status", previous_status)).upper()
    if not verified_content or re.search(r"\\bten shots on target\\b", verified_content, re.I):
        verified = None
        for status in ("scheduled", "live", "draft"):
            verify_response = session.get(base, params={
                "status": status.upper(), "maxResults": 100, "fetchBodies": "true"
            }, headers=headers, timeout=30)
            verify_response.raise_for_status()
            found = [item for item in verify_response.json().get("items", [])
                     if str(item.get("id", "")) == post_id]
            if found:
                verified = found[0]
                break
        if verified is None:
            raise RuntimeError("Updated Blogger post could not be found for verification.")
        verified_content = str(verified.get("content", ""))
        current_status = str(verified.get("status", previous_status)).upper()
    if re.search(r"\\bten shots on target\\b", verified_content, re.I):
        raise RuntimeError("Blogger update returned, but the incorrect wording is still present.")

    if previous_status == "SCHEDULED" and current_status not in {"SCHEDULED", "LIVE"}:
        if not original_publish_time:
            raise RuntimeError("Scheduled post status changed and original publish time is unavailable.")
        publish_response = session.post(
            f"{base}/{quote(post_id, safe='')}/publish",
            params={"publishDate": original_publish_time},
            headers=headers, timeout=45
        )
        publish_response.raise_for_status()
        verify_response = session.get(f"{base}/{quote(post_id, safe='')}",
            params={"fetchImages": "true", "fetchBody": "true"},
            headers=headers, timeout=45)
        verify_response.raise_for_status()
        verified = verify_response.json()
        current_status = str(verified.get("status", "")).upper()

    print("CORRECTION_SUCCESS")
    print(f"post_id={post_id}")
    print(f"status={current_status}")
    print(f"url={verified.get('url', '')}")
    print(f"corrected_phrase_count={replacements}")
    print("corrected_fact=10 shots across five appearances (not 10 shots on target)")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"CORRECTION_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
