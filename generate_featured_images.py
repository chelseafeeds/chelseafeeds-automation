#!/usr/bin/env python3
"""Use a relevant image embedded in the source article; otherwise use the ChelseaFeeds logo."""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps

ROOT = Path(".")
OUTPUT = Path("output")
IMAGES = OUTPUT / "images"
WIDTH, HEIGHT = 1200, 675
TIMEOUT = (5, 30)
BLOG_LOGO_URL = "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEj_LQEuUeApFnfW2_XrI4dtabQ38WxGe6ULYAvXpFm1XRpbsgd2oEM8x_F-X5I4PzOuBJIqEit80zFgQXXL05oL6ycNSTC1wb9bR0dvlwowu6Z9upUx1zZy15KI5SPFlqRP9zLXR9B37OTjpfbchhhT97kySZ5I36Vh9Qj0LNB_-yMw7pRHbLOo9I_Ceu8/w800/1001060295.png"
HEADERS = {
    "User-Agent": "ChelseaFeedsImageBot/2.0 (https://www.chelseafeeds.com/; editorial image sourcing)"
}
STOP = {
    "football", "fc", "club", "latest", "news", "report", "reports",
    "match", "preview", "analysis", "reaction", "what", "why", "how", "the",
    "and", "for", "with", "from", "after", "before", "over", "under", "into",
    "this", "that", "their", "they", "will", "could", "should", "has", "have",
    "was", "are", "its", "new", "season", "premier", "league"
}
ALLOWED_LICENSES = (
    re.compile(r"^CC BY (2\.0|3\.0|4\.0)$", re.I),
    re.compile(r"^CC0", re.I),
    re.compile(r"^Public Domain Mark 1\.0$", re.I),
)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def tokens(value):
    return {
        word for word in re.findall(r"[a-z0-9]{3,}", str(value or "").lower())
        if word not in STOP and not word.isdigit()
    }


def clean_meta(value):
    return re.sub(r"\s+", " ", BeautifulSoup(html.unescape(str(value or "")), "html.parser").get_text(" ", strip=True)).strip()


def license_allowed(name):
    name = clean_meta(name)
    return any(pattern.search(name) for pattern in ALLOWED_LICENSES)


def relevance_score(story, draft, candidate_text):
    article_terms = tokens(" ".join([
        story.get("title", ""), draft.get("title", ""),
        story.get("best_summary", ""), draft.get("excerpt", "")
    ]))
    candidate_terms = tokens(candidate_text)
    overlap = article_terms & candidate_terms
    # Require at least two meaningful shared terms; a generic football image is not enough.
    score = len(overlap)
    if "chelsea" in candidate_terms and "chelsea" in tokens(" ".join([
        story.get("title", ""), draft.get("title", "")
    ])):
        score += 1
    return score, sorted(overlap)


def process_one(story, draft, images_dir, session, api_key, source_images=None):
    """Prefer a clearly relevant image embedded in the original article; fall back to the supplied blog logo."""
    base = re.sub(r"[^a-z0-9]+", "-", str(draft.get("title") or story.get("title") or "chelsea-story").lower()).strip("-")[:75]
    images_dir.mkdir(parents=True, exist_ok=True)
    article_terms = tokens(" ".join([story.get("title", ""), draft.get("title", ""), draft.get("excerpt", "")]))
    ranked = []
    for candidate in (source_images or []):
        url = str(candidate.get("url") or "")
        if not url.startswith("https://"):
            continue
        alt = " ".join([candidate.get("alt_text", ""), candidate.get("caption", "")])
        score, matched = relevance_score(story, draft, alt)
        if score >= 2:
            ranked.append((score, candidate, matched))
    source_type = "article_source_photo"
    if ranked:
        ranked.sort(key=lambda x: x[0], reverse=True)
        _, candidate, matched = ranked[0]
        image_url = candidate["url"]
        credit_text = "Image from the original article source"
        source_page = candidate.get("page_url", "")
        alt_text = candidate.get("alt_text") or candidate.get("caption") or draft.get("title", "")
    else:
        candidate = {}
        image_url = BLOG_LOGO_URL
        matched = []
        credit_text = "ChelseaFeeds blog logo"
        source_page = "https://www.chelseafeeds.com/"
        alt_text = "ChelseaFeeds"
        source_type = "chelseafeeds_blog_logo"
    response = session.get(image_url, timeout=TIMEOUT)
    response.raise_for_status()
    if not response.headers.get("content-type", "").lower().startswith("image/"):
        raise RuntimeError("Selected featured-image URL did not return an image.")
    if len(response.content) < 1000 or len(response.content) > 12 * 1024 * 1024:
        raise RuntimeError("Selected featured image size is outside the allowed range.")
    filename = f"chelseafeeds-{base}-featured.jpg"
    destination = images_dir / filename
    temp = destination.with_suffix(".source")
    temp.write_bytes(response.content)
    try:
        with Image.open(temp) as source:
            source.verify()
        with Image.open(temp) as source:
            image = ImageOps.fit(source.convert("RGB"), (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS)
            image.save(destination, "JPEG", quality=91, optimize=True, progressive=True)
    finally:
        temp.unlink(missing_ok=True)
    return {
        "role": "thumbnail", "filename": filename, "local_path": f"images/{filename}",
        "alt_text": clean_meta(alt_text)[:250], "width": WIDTH, "height": HEIGHT,
        "bytes": destination.stat().st_size, "source_type": source_type,
        "source_page": source_page, "source_image_url": image_url,
        "credit_text": credit_text, "matched_terms": matched,
        "relevance_score": len(matched), "branded": source_type == "chelseafeeds_blog_logo"
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="output")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        assert relevance_score({"title":"Chelsea Sunderland penalty"}, {"title":"Chelsea Sunderland match report"},
                               "Chelsea Sunderland football penalty")[0] >= 2
        assert relevance_score({"title":"Chelsea injury update"}, {"title":"Chelsea injury update"},
                               "Generic stadium photo")[0] == 0
        assert BLOG_LOGO_URL.startswith("https://")
        print("PASS: relevant source-photo selection and ChelseaFeeds logo fallback")
        return

    output = Path(args.output)
    drafts_path = output / "article_drafts.json"
    ready_path = output / "blogger_ready_posts.json"
    if not drafts_path.is_file() or not ready_path.is_file():
        raise RuntimeError("Expected article_drafts.json and blogger_ready_posts.json in the processor output directory.")
    drafts_payload = json.loads(drafts_path.read_text(encoding="utf-8"))
    ready_payload = json.loads(ready_path.read_text(encoding="utf-8"))
    draft_items = drafts_payload.get("drafts", [])
    ready_posts = ready_payload.get("posts", [])
    stories_by_id = {}
    for item in draft_items:
        event_id = str(item.get("event_id", ""))
        stories_by_id[event_id] = {
            "title": item.get("collector_headline", ""),
            "story_type": item.get("story_type", ""),
            "team_scope": item.get("team_scope", ""),
            "best_summary": item.get("draft", {}).get("excerpt", ""),
            "source_images": item.get("source_images", [])
        }
    session = requests.Session()
    session.headers.update(HEADERS)
    api_key = ""
    counts = {}
    eligible_posts = []
    held_for_image_review = []
    for image_index, post in enumerate(ready_posts, start=1):
        event_id = str(post.get("event_id", ""))
        item = next((x for x in draft_items if str(x.get("event_id", "")) == event_id), None)
        if not item:
            held_for_image_review.append({"event_id": event_id, "reason": "matching article draft not found"})
            continue
        story = stories_by_id.get(event_id, {})
        draft = item.get("draft", {})
        logging.info("Featured image %d/%d: %s", image_index, len(ready_posts), str(draft.get("title") or event_id)[:130])
        try:
            asset = process_one(story, draft, output / "images", session, api_key, item.get("source_images", []))
        except Exception as exc:
            reason = type(exc).__name__ + ": " + str(exc)[:250]
            logging.warning("Holding %s: featured image unavailable (%s)", event_id, reason)
            held_for_image_review.append({"event_id": event_id, "reason": reason})
            draft["image_generation_status"] = "featured_image_error"
            draft["image_note"] = "Featured image could not be created; article was not sent to Blogger."
            continue
        logging.info("Featured image complete for %s: %s", event_id, asset.get("source_type"))
        counts[asset["source_type"]] = counts.get(asset["source_type"], 0) + 1
        draft["image_assets"] = [asset]
        draft["featured_image"] = asset
        draft["image_generation_status"] = asset["source_type"]
        draft["image_note"] = "Relevant source-article photo used when available; otherwise the ChelseaFeeds blog logo was used."
        post["featured_image"] = asset
        post["image_alt_text"] = asset["alt_text"]
        eligible_posts.append(post)
    ready_payload["posts"] = eligible_posts
    drafts_path.write_text(json.dumps(drafts_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ready_path.write_text(json.dumps(ready_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path = output / "editorial_report.json"
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["thumbnail_method"] = "relevant_source_article_photo_else_chelseafeeds_blog_logo"
        report["featured_image_pipeline"] = {
            "image_counts": counts,
            "eligible_blogger_posts": len(eligible_posts),
            "held_for_image_review": held_for_image_review,
            "fallback_policy": "Use the ChelseaFeeds blog logo when no relevant source-article image is available.",
            "source_photo_attribution_saved": True
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "featured_image_pipeline": "relevant_article_photo_else_chelseafeeds_blog_logo",
        "image_counts": counts, "eligible_blogger_posts": len(eligible_posts),
        "held_for_image_review": held_for_image_review, "images_directory": str(output / "images"),
        "fallback_policy": "ChelseaFeeds blog logo is used whenever no relevant image is available in the source article."
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
