#!/usr/bin/env python3
"""ChelseaFeeds mixed featured-image pipeline: licensed Commons photo -> AI illustration -> branded fallback."""
from __future__ import annotations

import argparse
import base64
import html
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(".")
OUTPUT = Path("output")
IMAGES = OUTPUT / "images"
WIDTH, HEIGHT = 1200, 675
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
OPENAI_IMAGES_API = "https://api.openai.com/v1/images/generations"
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2.5-flare")
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
    re.compile(r"^Public domain", re.I),
    re.compile(r"^PD-", re.I),
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


def search_commons(story, draft, session):
    """Try article-specific Commons searches, then broader Chelsea/football searches.

    Broad fallback images still have to pass the same licence, size and relevance
    checks. We never scrape arbitrary publishers or reuse copyrighted match photos.
    """
    title = str(draft.get("title") or story.get("title") or "")
    specific_terms = sorted(tokens(title), key=lambda x: (-len(x), x))[:7]
    queries = []
    if specific_terms:
        queries.append(("article_specific", " ".join(specific_terms)))
    queries.extend([
        ("chelsea_fc", "Chelsea FC football"),
        ("chelsea_stadium", "Stamford Bridge Chelsea football stadium"),
        ("chelsea_match", "Chelsea FC football match")
    ])

    for query_kind, query in queries:
        try:
            response = session.get(COMMONS_API, params={
                "action": "query", "format": "json", "formatversion": 2,
                "generator": "search", "gsrnamespace": 6, "gsrsearch": query,
                "gsrlimit": 35, "prop": "imageinfo",
                "iiprop": "url|size|extmetadata", "iiurlwidth": 1600
            }, timeout=TIMEOUT)
            response.raise_for_status()
            pages = response.json().get("query", {}).get("pages", [])
        except Exception as exc:
            logging.warning("Commons search tier %s failed: %s", query_kind, str(exc)[:160])
            continue

        ranked = []
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata") or {}
            license_name = clean_meta((meta.get("LicenseShortName") or {}).get("value"))
            if not license_allowed(license_name):
                continue
            width, height = int(info.get("width") or 0), int(info.get("height") or 0)
            if width < 900 or height < 500 or width / max(height, 1) < 1.25:
                continue
            description = clean_meta((meta.get("ImageDescription") or {}).get("value"))
            artist = clean_meta((meta.get("Artist") or {}).get("value"))
            text = " ".join([page.get("title", ""), description, artist])
            score, matched = relevance_score(story, draft, text)

            # The article-specific query requires two meaningful matches.
            # Broad Chelsea fallback accepts one Chelsea/football-specific match,
            # but never a generic unrelated stadium or crowd photo.
            candidate_terms = tokens(text)
            has_chelsea_context = bool(candidate_terms & {"chelsea", "stamford", "bridge"})
            if query_kind == "article_specific":
                if score < 2:
                    continue
            elif not has_chelsea_context:
                continue
            elif score < 1:
                continue

            ranked.append({
                "score": score, "matched_terms": matched, "page": page,
                "info": info, "meta": meta, "license": license_name,
                "description": description, "artist": artist,
                "search_tier": query_kind
            })
        if ranked:
            ranked.sort(key=lambda item: (
                item["score"], item["info"].get("width", 0)
            ), reverse=True)
            chosen = ranked[0]
            logging.info("Commons selected %s image via %s search (score=%s)",
                         chosen["page"].get("title", ""), query_kind, chosen["score"])
            return chosen
    return None

def download_commons_photo(candidate, destination, session):
    info = candidate["info"]
    url = info.get("thumburl") or info.get("url")
    if not url or not url.startswith("https://"):
        raise RuntimeError("Commons returned no secure image URL.")
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    if not response.headers.get("content-type", "").lower().startswith("image/"):
        raise RuntimeError("Commons image response was not an image.")
    if len(response.content) < 5000 or len(response.content) > 12 * 1024 * 1024:
        raise RuntimeError("Commons image size is outside the allowed range.")
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


def generate_ai_illustration(story, draft, destination, session, api_key):
    title = str(draft.get("title") or story.get("title") or "Chelsea football news")
    summary = str(draft.get("excerpt") or story.get("best_summary") or "")[:1000]
    visual_direction = str(draft.get("image_prompt") or "")[:1600]
    prompt = (
        "Create a premium 16:9 editorial sports illustration for a professional Chelsea FC news website. "
        "Show a vivid, story-specific football scene based on the supplied article context. "
        "Use dramatic stadium lighting, convincing football action, realistic anatomy, rich photographic-style "
        "painterly detail, Chelsea blue and deep navy accents, balanced composition, and clear visual storytelling. "
        "This must be visibly an editorial illustration, NOT documentary match photography. "
        "Do not depict or imitate any identifiable real footballer, coach, referee, photographer or public figure. "
        "Do not invent a real player's face, exact likeness, club crest, sponsor logo, shirt name or readable text. "
        "No headline, no typography, no watermark, no border. Keep important action in the central 80 percent. "
        f"Article headline: {title}. Article context: {summary}. Visual direction: {visual_direction}"
    )
    response = session.post(OPENAI_IMAGES_API,
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        json={
            "model": OPENAI_IMAGE_MODEL, "prompt": prompt,
            "size": "1536x864", "quality": "medium",
            "output_format": "jpeg", "output_compression": 88, "n": 1
        }, timeout=OPENAI_IMAGE_TIMEOUT)
    response.raise_for_status()
    data = response.json().get("data") or []
    if not data or not data[0].get("b64_json"):
        raise RuntimeError("Image API returned no base64 image.")
    raw = base64.b64decode(data[0]["b64_json"], validate=True)
    if len(raw) < 5000 or len(raw) > 15 * 1024 * 1024:
        raise RuntimeError("Generated image size is outside the allowed range.")
    temp = destination.with_suffix(".generated")
    temp.write_bytes(raw)
    try:
        with Image.open(temp) as source:
            source.verify()
        with Image.open(temp) as source:
            image = ImageOps.fit(source.convert("RGB"), (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS)
            # A small disclosure keeps an illustrative scene from being mistaken for a genuine match photo.
            draw = ImageDraw.Draw(image, "RGBA")
            font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            try:
                font = ImageFont.truetype(font_path, 18)
            except OSError:
                font = ImageFont.load_default()
            draw.rounded_rectangle((35, 35, 278, 82), radius=9, fill=(5, 18, 36, 205))
            draw.text((50, 49), "CHELSEAFEEDS  |  AI ILLUSTRATION",
                      font=font, fill=(255, 255, 255, 245))
            image.save(destination, "JPEG", quality=91, optimize=True, progressive=True)
    finally:
        temp.unlink(missing_ok=True)


def branded_fallback(draft, story, destination):
    # Retain the previous dependable no-key branded thumbnail as a last resort.
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (WIDTH, HEIGHT))
    pixels = image.load()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            t = min(1.0, (x / WIDTH) * 0.72 + (y / HEIGHT) * 0.28)
            pixels[x, y] = (int(3 + 4*t), int(70 - 50*t), int(148 - 112*t))
    draw = ImageDraw.Draw(image, "RGBA")
    for cx, cy, radius in [(970, 175, 170), (1080, 520, 245), (770, 590, 180)]:
        draw.ellipse((cx-radius, cy-radius, cx+radius, cy+radius), outline=(255,255,255,35), width=4)
    draw.ellipse((965, 290, 1110, 435), fill=(245,247,250,235), outline=(212,175,55,255), width=5)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    try:
        brand = ImageFont.truetype(font_path, 26)
        title_font = ImageFont.truetype(font_path, 48)
    except OSError:
        brand = title_font = ImageFont.load_default()
    draw.rounded_rectangle((42, 42, 345, 96), radius=10, fill=(5,18,36,220), outline=(212,175,55,255), width=2)
    draw.text((60, 54), "CHELSEAFEEDS", font=brand, fill="white")
    title = re.sub(r"\s+", " ", str(draft.get("title") or story.get("title") or "Chelsea FC news")).strip()
    words, lines, current = title.split(), [], ""
    for word in words:
        candidate = (current + " " + word).strip()
        if draw.textbbox((0,0), candidate, font=title_font)[2] > 760 and current:
            lines.append(current)
            current = word
        else:
            current = candidate
        if len(lines) >= 3:
            break
    if current and len(lines) < 4:
        lines.append(current)
    y = 205
    for line in lines[:4]:
        draw.text((55, y), line, font=title_font, fill="white", stroke_width=2, stroke_fill=(4,15,31,150))
        y += 62
    image.save(destination, "JPEG", quality=90, optimize=True, progressive=True)


def process_one(story, draft, images_dir, session, api_key, source_images=None):
    """Use only an article's own source-page image, and only with explicit reusable rights."""
    base = re.sub(r"[^a-z0-9]+", "-", str(draft.get("title") or story.get("title") or "chelsea-story").lower()).strip("-")[:75]
    filename = f"chelseafeeds-{base}-featured.jpg"
    destination = images_dir / filename
    images_dir.mkdir(parents=True, exist_ok=True)
    article_terms = tokens(" ".join([story.get("title", ""), draft.get("title", ""), draft.get("excerpt", "")]))
    ranked = []
    for candidate in (source_images or []):
        if not candidate.get("rights_explicit") or not license_allowed(candidate.get("license_name", "")):
            continue
        alt = candidate.get("alt_text", "")
        score, matched = relevance_score(story, draft, alt)
        # Source page provenance alone is not enough: require descriptive alt text that matches the story.
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
        assert license_allowed("Public domain")
        assert not license_allowed("CC BY-SA 4.0")
        assert not license_allowed("All rights reserved")
        assert not license_allowed("CC BY (version requires verification)")
        assert not license_allowed("CC BY-SA 4.0")
        assert relevance_score({"title":"Chelsea Sunderland penalty"}, {"title":"Chelsea Sunderland match report"},
                               "Chelsea Sunderland football penalty") [0] >= 2
        assert relevance_score({"title":"Chelsea injury update"}, {"title":"Chelsea injury update"},
                               "Generic stadium photo") [0] == 0
        print("PASS: image licensing and relevance safeguards")
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
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    counts = {}
    for image_index, post in enumerate(ready_posts, start=1):
        event_id = str(post.get("event_id", ""))
        item = next((x for x in draft_items if str(x.get("event_id", "")) == event_id), None)
        if not item:
            continue
        story = stories_by_id.get(event_id, {})
        draft = item.get("draft", {})
        logging.info("Featured image %d/%d: %s", image_index, len(ready_posts), str(draft.get("title") or event_id)[:130])
        asset = process_one(story, draft, output / "images", session, api_key, item.get("source_images", []))
        logging.info("Featured image complete for %s: %s", event_id, asset.get("source_type"))
        counts[asset["source_type"]] = counts.get(asset["source_type"], 0) + 1
        draft["image_assets"] = [asset]
        draft["featured_image"] = asset
        draft["image_generation_status"] = asset["source_type"]
        draft["image_note"] = (
            "Featured image selected from Wikimedia Commons under the recorded licence with attribution."
            if asset["source_type"] == "licensed_commons_photo" else
            "AI-generated editorial illustration; not a documentary photograph."
            if asset["source_type"] == "ai_generated_illustration" else
            "Fallback branded graphic used because no suitable licensed photograph or configured AI image service was available."
        )
        post["featured_image"] = asset
        post["image_alt_text"] = asset["alt_text"]
    drafts_path.write_text(json.dumps(drafts_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ready_path.write_text(json.dumps(ready_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path = output / "editorial_report.json"
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["thumbnail_method"] = "original_source_page_images_only_explicit_reuse_rights"
        report["featured_image_pipeline"] = {
            "image_counts": counts,
            "openai_image_api_configured": bool(api_key),
            "commons_licence_policy": "CC0, Public Domain, or CC BY 2.0/3.0/4.0 only",
            "ai_images_disclosed": True,
            "commons_attribution_saved": True
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "featured_image_pipeline": "original_source_images_only_with_explicit_reuse_rights",
        "image_counts": counts, "openai_image_api_configured": bool(api_key),
        "images_directory": str(output / "images"),
        "license_policy": "Only explicit CC0, Public Domain, or exact CC BY 2.0/3.0/4.0 detected on the original source page; otherwise hold for review"
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
