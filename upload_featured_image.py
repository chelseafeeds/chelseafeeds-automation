#!/usr/bin/env python3
"""Publish a generated article thumbnail to this public repository and verify its URL."""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

INPUT_DIR = Path(os.getenv("BLOGGER_INPUT_DIR", "input"))
READY_FILE = INPUT_DIR / "blogger_ready_posts.json"
IMAGE_ROOT = INPUT_DIR.resolve()

def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True)

def main():
    payload = json.loads(READY_FILE.read_text(encoding="utf-8"))
    posts = payload.get("posts", []) if isinstance(payload, dict) else payload
    event_id = os.environ["ARTICLE_EVENT_ID"].strip()
    matches = [p for p in posts if str(p.get("event_id", "")) == event_id]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one article for event_id={event_id}; found {len(matches)}")

    post = matches[0]
    featured = post.get("featured_image") or {}
    filename = str(featured.get("filename", "")).strip()
    local_path = str(featured.get("local_path", "")).strip()
    if not filename or not re.fullmatch(r"[A-Za-z0-9._-]+\.(?:jpg|jpeg|png|webp)", filename, re.I):
        raise RuntimeError("Featured image filename is missing or unsafe.")
    if local_path != f"images/{filename}":
        raise RuntimeError("Featured image path does not match the expected images/<filename> layout.")
    image_path = (INPUT_DIR / local_path).resolve()
    if IMAGE_ROOT not in image_path.parents or not image_path.is_file():
        raise RuntimeError(f"Featured image file is missing or outside the downloaded artifact: {local_path}")
    if image_path.stat().st_size < 1000 or image_path.stat().st_size > 10 * 1024 * 1024:
        raise RuntimeError("Featured image size is outside the permitted 1 KB–10 MB range.")
    if featured.get("width") != 1200 or featured.get("height") != 675:
        raise RuntimeError("Featured image dimensions must be 1200 x 675.")
    if image_path.suffix.lower() in {".jpg", ".jpeg"} and image_path.read_bytes()[:3] != b"\xff\xd8\xff":
        raise RuntimeError("Featured image does not contain a valid JPEG signature.")

    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    branch = os.environ.get("GITHUB_REF_NAME", "main").strip()
    if repository != "chelseafeeds/chelseafeeds-automation":
        raise RuntimeError("Refusing to publish image outside the configured ChelseaFeeds repository.")
    if not run_id.isdigit() or branch != "main":
        raise RuntimeError("Image publishing is restricted to the main branch and a valid Actions run.")

    destination = Path("assets/blogger-images") / f"{run_id}-{filename}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(image_path, destination)

    run("git", "config", "user.name", "github-actions[bot]")
    run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    run("git", "add", str(destination))
    staged = run("git", "diff", "--cached", "--name-only").stdout.strip()
    if staged:
        run("git", "commit", "-m", f"Add ChelseaFeeds featured image for run {run_id}")
        run("git", "push", "origin", f"HEAD:refs/heads/{branch}")
    commit = run("git", "rev-parse", "HEAD").stdout.strip()
    image_url = f"https://raw.githubusercontent.com/{repository}/{commit}/{destination.as_posix()}"

    verified = False
    last_error = None
    for attempt in range(4):
        try:
            response = requests.get(image_url, timeout=30)
            if response.status_code == 200 and response.headers.get("content-type", "").lower().startswith("image/"):
                verified = True
                break
            last_error = f"HTTP {response.status_code}, content-type={response.headers.get('content-type', '')}"
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(2 * (attempt + 1))
    if not verified:
        raise RuntimeError(f"Uploaded image URL could not be verified publicly: {last_error}")

    alt = str(featured.get("alt_text") or post.get("image_alt_text") or post.get("title") or "ChelseaFeeds article image").strip()
    alt = re.sub(r"\s+", " ", alt)[:250]
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        raise RuntimeError("GITHUB_OUTPUT is unavailable; cannot pass the verified image URL to the publisher.")
    with open(output_file, "a", encoding="utf-8") as out:
        out.write(f"image_url={image_url}\n")
        out.write(f"alt_text={alt.replace(chr(10), ' ').replace(chr(13), ' ')}\n")
    print(json.dumps({"featured_image_status": "uploaded_and_publicly_verified",
                      "image_url": image_url, "filename": filename,
                      "width": 1200, "height": 675}, ensure_ascii=False))

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FEATURED_IMAGE_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
