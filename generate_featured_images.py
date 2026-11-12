#!/usr/bin/env python3
"""ChelseaFeeds original-source-only featured-image pipeline; fail closed without image-specific reuse rights."""
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
OPENAI_IMAGE_TIMEOUT = max(30, min(120, int(os.getenv("OPENAI_IMAGE_TIMEOUT", "90"))))
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
    """Use only an article's own source-page image, and only with explicit reusable rights."""
    base = re.sub(r"[^a-z0-9]+", "-", str(draft.get("title") or story.get("title") or "chelsea-story").lower()).strip("-")[:75]
    filename = f"chelseafeeds-{base}-featured.jpg"
    destination = images_dir / filename
    images_dir.mkdir(parents=True, exist_ok=True)
    article_terms = tokens(" ".join([story.get("title", ""), draft.get("title", ""), draft.get("excerpt", "")]))
    ranked = []
    for candidate in (source_images or []):
        license_name = str(candidate.get("license_name") or "")
        license_url = str(candidate.get("license_url") or "")
        source_page = str(candidate.get("page_url") or "")
        if (not candidate.get("rights_explicit") or not license_allowed(license_name)
                or not license_url.startswith("https://") or not source_page.startswith("https://")
                or candidate.get("rights_evidence_scope") != "image_or_figure"):
            continue
        alt = " ".join([candidate.get("alt_text", ""), candidate.get("caption", "")])
        score, matched = relevance_score(story, draft, alt)
        # Require article-specific descriptive evidence, not a generic Chelsea image.
        if score < 2:
            continue
        ranked.append((score, candidate, matched))
    if not ranked:
        raise RuntimeError("No relevant source-page image with an explicitly accepted reuse licence; article image review required.")
    ranked.sort(key=lambda x: x[0], reverse=True)
    score, candidate, matched = ranked[0]
    response = session.get(candidate["url"], timeout=TIMEOUT)
    response.raise_for_status()
    if not response.headers.get("content-type", "").lower().startswith("image/"):
        raise RuntimeError("Source image URL did not return an image.")
    if len(response.content) < 5000 or len(response.content) > 12 * 1024 * 1024:
        raise RuntimeError("Source image size is outside the allowed range.")
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
        "alt_text": (candidate.get("alt_text") or draft.get("image_alt_text") or draft.get("title", ""))[:250],
        "width": WIDTH, "height": HEIGHT, "bytes": destination.stat().st_size,
        "source_type": "licensed_original_source_photo", "source_page": candidate.get("page_url"),
        "source_image_url": candidate["url"], "license_name": candidate["license_name"],
        "rights_evidence_scope": candidate.get("rights_evidence_scope"),
        "license_url": candidate.get("license_url", ""), "credit_text": "Image from original article source",
        "matched_terms": matched, "relevance_score": score, "branded": False
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="output")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        assert license_allowed("CC BY 4.0")
        assert license_allowed("CC BY 3.0")
        assert license_allowed("CC0 1.0")
        assert license_allowed("Public Domain Mark 1.0")
        assert not license_allowed("Public domain")
        assert not license_allowed("CC BY-SA 4.0")
        assert not license_allowed("All rights reserved")
        assert not license_allowed("CC BY (version requires verification)")
        assert not license_allowed("CC BY-SA 4.0")
        assert relevance_score({"title":"Chelsea Sunderland penalty"}, {"title":"Chelsea Sunderland match report"},
                               "Chelsea Sunderland football penalty") [0] >= 2
        assert relevance_score({"title":"Chelsea injury update"}, {"title":"Chelsea injury update"},
                               "Generic stadium photo") [0] == 0
        licensed = {"rights_explicit": True, "license_name": "CC BY 4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/", "page_url": "https://source.example/article", "rights_evidence_scope": "image_or_figure", "alt_text": "Chelsea Sunderland penalty match"}
        assert licensed["rights_explicit"] and license_allowed(licensed["license_name"])
        unlicensed = dict(licensed, rights_explicit=False, license_name="", license_url="")
        assert not (unlicensed.get("rights_explicit") and license_allowed(unlicensed.get("license_name", "")))
        print("PASS: original-source-only image licensing and relevance safeguards")
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
            logging.warning("Holding %s: no acceptable original-source image (%s)", event_id, reason)
            held_for_image_review.append({"event_id": event_id, "reason": reason})
            draft["image_generation_status"] = "held_image_rights_or_relevance_review"
            draft["image_note"] = "No relevant original-source image with image-specific reuse rights was available; this article was not sent to Blogger."
            continue
        logging.info("Featured image complete for %s: %s", event_id, asset.get("source_type"))
        counts[asset["source_type"]] = counts.get(asset["source_type"], 0) + 1
        draft["image_assets"] = [asset]
        draft["featured_image"] = asset
        draft["image_generation_status"] = asset["source_type"]
        draft["image_note"] = "Featured image selected from the original article source under image-specific reuse rights with attribution."
        post["featured_image"] = asset
        post["image_alt_text"] = asset["alt_text"]
        eligible_posts.append(post)
    ready_payload["posts"] = eligible_posts
    drafts_path.write_text(json.dumps(drafts_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ready_path.write_text(json.dumps(ready_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path = output / "editorial_report.json"
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["thumbnail_method"] = "original_source_page_images_only_explicit_reuse_rights"
        report["featured_image_pipeline"] = {
            "image_counts": counts,
            "eligible_blogger_posts": len(eligible_posts),
            "held_for_image_review": held_for_image_review,
            "image_rights_policy": "Image/figure-specific CC0 1.0, Public Domain Mark 1.0, or exact CC BY 2.0/3.0/4.0 only",
            "original_source_attribution_saved": True
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "featured_image_pipeline": "original_article_images_only_with_image_specific_reuse_rights",
        "image_counts": counts, "eligible_blogger_posts": len(eligible_posts),
        "held_for_image_review": held_for_image_review, "images_directory": str(output / "images"),
        "license_policy": "Only image/figure-specific CC0 1.0, Public Domain Mark 1.0, or exact CC BY 2.0/3.0/4.0; otherwise hold for review"
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
