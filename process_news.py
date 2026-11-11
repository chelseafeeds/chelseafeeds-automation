#!/usr/bin/env python3
"""ChelseaFeeds v1.2: evidence triage, editorial quality gates and branded image assets."""
from __future__ import annotations
import argparse, base64, json, logging, os, re, time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

VERSION = "1.2"
TIMEOUT = 20
MAX_FETCH_CHARS = 9000
MAX_DRAFTS = max(0, min(3, int(os.getenv("MAX_DRAFTS_PER_RUN", "3"))))
AI_PROVIDER = os.getenv("AI_PROVIDER", "groq").strip().lower()
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
MODEL = os.getenv("AI_MODEL", "openai/gpt-oss-120b").strip()
MAX_SOURCE_PACK = int(os.getenv("MAX_SOURCE_PACK", "4"))
MAX_SOURCE_CHARS = int(os.getenv("MAX_SOURCE_CHARS", "3500"))
IMAGE_PROVIDER = os.getenv("IMAGE_PROVIDER", "cloudflare").strip().lower()
CF_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CF_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
CF_MODEL = os.getenv("CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell").strip()
MAX_IMAGES_PER_RUN = max(0, min(6, int(os.getenv("MAX_IMAGES_PER_RUN", "6"))))
IMAGE_STEPS = max(1, min(4, int(os.getenv("IMAGE_STEPS", "4"))))
OFFICIAL = {"chelseafc.com", "premierleague.com", "uefa.com", "thefa.com", "fifa.com", "efl.com"}
ESTABLISHED = {"bbc.co.uk", "bbc.com", "skysports.com", "theguardian.com", "reuters.com", "apnews.com", "espn.com", "independent.co.uk", "standard.co.uk", "nytimes.com", "theathletic.com", "football365.com", "thetimes.com", "telegraph.co.uk"}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ChelseaFeedsEditorialBot/1.2; +https://www.chelseafeeds.com/)"}
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

def utcnow():
    return datetime.now(timezone.utc)

def host(url):
    return (urlparse(url or "").hostname or "").lower().removeprefix("www.")

def in_domains(url, domains):
    d = host(url)
    return any(d == x or d.endswith("." + x) for x in domains)

def slug(value):
    value = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return (value[:70].strip("-") or "chelsea-story")

def clean_text(html):
    soup = BeautifulSoup(html or "", "html.parser")
    for node in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        node.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for selector in ['meta[property="og:title"]', 'meta[name="twitter:title"]']:
        node = soup.select_one(selector)
        if node and node.get("content"):
            title = node["content"].strip()
            break
    desc = ""
    for selector in ['meta[property="og:description"]', 'meta[name="description"]']:
        node = soup.select_one(selector)
        if node and node.get("content"):
            desc = node["content"].strip()
            break
    article = soup.find("article") or soup.find("main") or soup.body or soup
    body = re.sub(r"\s+", " ", article.get_text(" ", strip=True))
    return title[:300], desc[:800], body[:MAX_FETCH_CHARS]

def fetch_source(url, session):
    result = {"url": url, "domain": host(url), "http_status": None, "accessible": False,
              "page_title": "", "description": "", "excerpt": "", "error": None}
    try:
        response = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        result["http_status"] = response.status_code
        result["final_url"] = response.url
        if response.status_code >= 400:
            result["error"] = "HTTP " + str(response.status_code)
            return result
        ctype = response.headers.get("content-type", "").lower()
        if "html" not in ctype and "text" not in ctype:
            result["error"] = "Source did not return an HTML/text page"
            return result
        title, desc, body = clean_text(response.text)
        result.update(page_title=title, description=desc, excerpt=body[:MAX_FETCH_CHARS])
        result["accessible"] = len(body) >= 180
        if not result["accessible"]:
            result["error"] = "Page returned too little readable text"
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)[:220]
    return result

def evidence_assessment(story, fetched):
    accessible = [x for x in fetched if x["accessible"]]
    accessible_domains = sorted({x["domain"] for x in accessible})
    official = [x for x in accessible if in_domains(x["url"], OFFICIAL)]
    established = [x for x in accessible if in_domains(x["url"], ESTABLISHED)]
    typ = story.get("story_type", "general_news")
    if official:
        tier = "official_source_accessible"
    elif len({x["domain"] for x in established}) >= 2:
        tier = "multiple_established_publishers_accessible"
    elif established:
        tier = "single_established_publisher_accessible"
    elif accessible:
        tier = "other_direct_source_accessible"
    else:
        tier = "no_usable_source"
    if typ == "opinion_analysis":
        eligible = bool(accessible)
        reason = "Opinion draft may be prepared with attribution; factual premises still require checking."
    elif typ in {"transfer_rumour", "confirmed_transfer"}:
        eligible = bool(accessible) and (bool(official) or len({x["domain"] for x in established}) >= 2)
        reason = "Transfer coverage requires official evidence or two accessible established publishers."
    else:
        eligible = bool(official) or len({x["domain"] for x in established}) >= 2
        reason = "Factual coverage requires an accessible official source or two distinct established publishers."
    return {"evidence_tier": tier, "accessible_source_count": len(accessible),
            "accessible_publisher_domains": accessible_domains, "official_source_count": len(official),
            "established_source_domains": sorted({x["domain"] for x in established}),
            "eligible_for_draft": eligible, "claims_verified": False, "reason": reason,
            "warning": "Accessibility and publisher count are evidence triage, not proof of factual accuracy."}

def build_prompt(story, evidence):
    pack = [{"url": x["url"], "domain": x["domain"], "page_title": x["page_title"],
             "description": x["description"], "source_excerpt": x["excerpt"][:MAX_SOURCE_CHARS]}
            for x in evidence[:MAX_SOURCE_PACK] if x["accessible"]]
    return {"story": {"headline": story.get("title"), "story_type": story.get("story_type"),
            "team_scope": story.get("team_scope"), "published": story.get("published"),
            "collector_summary": story.get("best_summary", ""),
            "original_source_links": story.get("source_urls", [])},
            "accessible_source_material": pack}

def generate_draft(story, evidence, api_key, session):
    payload = build_prompt(story, evidence)
    system = (
        "You are a careful Chelsea FC sports journalist and SEO editor. Use ONLY supplied evidence. "
        "Never invent facts, quotes, dates, scores, stats, injuries, fees, contract clauses, line-ups, "
        "venues or motives. Do not create direct quotations; paraphrase and attribute instead. "
        "Do not copy source wording. Separate confirmed facts, reported claims and opinion. "
        "For transfers, state clearly whether confirmed or reported; never infer fees or clauses. "
        "For opinion, attribute the view and distinguish its factual premises. Keep men, women and "
        "academy teams distinct. Write concise, substantive copy; no padding to hit a word count. "
        "Use useful H2 headings, clear intro, original analysis, and a Sources heading at the end. "
        "Include ONLY supplied source URLs in the Sources section. Do not invent internal links. "
        "Return ONLY JSON keys: title, search_description, focus_keyword, labels, article_html, "
        "excerpt, image_prompt, image_alt_text, editorial_notes, unsupported_or_unverified_points. "
        "HTML must be Blogger-ready, with no scripts, iframe, form, schema or inline ads. "
        "Image prompt must request an editorial illustration, not fake documentary match photography "
        "or a photorealistic depiction of a real player. Chelsea blue #034694, navy #071426, white, gold."
    )
    req = {"model": MODEL, "response_format": {"type": "json_object"},
           "messages": [{"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]}
    response = session.post(AI_BASE_URL + "/chat/completions",
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        json=req, timeout=90)
    response.raise_for_status()
    draft = json.loads(response.json()["choices"][0]["message"]["content"])
    required = {"title", "search_description", "focus_keyword", "labels", "article_html",
                "excerpt", "image_prompt", "image_alt_text", "editorial_notes", "unsupported_or_unverified_points"}
    missing = required - set(draft)
    if missing:
        raise ValueError("Model output missing keys: " + ", ".join(sorted(missing)))
    return draft

def quality_gate(draft, fetched, story):
    html = str(draft.get("article_html", ""))
    soup = BeautifulSoup(html, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    links = {a.get("href") for a in soup.find_all("a", href=True)}
    allowed_urls = {x["url"] for x in fetched if x["accessible"]}
    checks = {
        "title_present_and_reasonable": 25 <= len(str(draft.get("title", "")).strip()) <= 100,
        "search_description_length": 100 <= len(str(draft.get("search_description", "")).strip()) <= 180,
        "substantive_article_body": len(text.split()) >= 300,
        "has_section_headings": len(soup.find_all(["h2", "h3"])) >= 2,
        "has_source_links": bool(links & allowed_urls),
        "source_links_are_supplied": links.issubset(allowed_urls),
        "no_unsafe_html": not bool(re.search(r"<(script|iframe|form|object|embed)\b", html, re.I)),
        "no_empty_required_seo_fields": all(str(draft.get(k, "")).strip() for k in ("focus_keyword", "excerpt", "image_prompt", "image_alt_text"))
    }
    score = round(100 * sum(checks.values()) / len(checks))
    failed = [k for k, ok in checks.items() if not ok]
    # The processor refuses low-substance, unsafe, or source-less drafts. It never declares facts verified.
    critical = {"substantive_article_body", "has_source_links", "source_links_are_supplied", "no_unsafe_html"}
    passed = not (critical & set(failed)) and score >= 75
    return {"passed": passed, "score": score, "checks": checks, "failed_checks": failed,
            "note": "Automated editorial screening only; it is not independent fact-checking."}

def cloudflare_image(prompt, session):
    if not (CF_ACCOUNT_ID and CF_API_TOKEN):
        raise RuntimeError("Cloudflare image service is not configured")
    endpoint = "https://api.cloudflare.com/client/v4/accounts/" + CF_ACCOUNT_ID + "/ai/run/" + CF_MODEL
    response = session.post(endpoint, headers={"Authorization": "Bearer " + CF_API_TOKEN,
        "Content-Type": "application/json"},
        json={"prompt": prompt[:1900], "steps": IMAGE_STEPS},
        timeout=180)
    response.raise_for_status()
    ctype = response.headers.get("content-type", "").lower()
    if "image/" in ctype:
        return response.content
    payload = response.json()
    result = payload.get("result", {})
    encoded = result.get("image") if isinstance(result, dict) else result
    if not encoded and isinstance(payload.get("result"), str):
        encoded = payload["result"]
    if not encoded:
        raise ValueError("Cloudflare response did not contain generated image data")
    return base64.b64decode(encoded)

def brand_image(raw, output_path):
    from io import BytesIO
    image = Image.open(BytesIO(raw)).convert("RGB")
    # Consistent landscape editorial crop and web-friendly output.
    target_ratio = 16 / 9
    ratio = image.width / image.height
    if ratio > target_ratio:
        new_w = int(image.height * target_ratio)
        left = (image.width - new_w) // 2
        image = image.crop((left, 0, left + new_w, image.height))
    else:
        new_h = int(image.width / target_ratio)
        top = (image.height - new_h) // 2
        image = image.crop((0, top, image.width, top + new_h))
    image = image.resize((1200, 675), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(image, "RGBA")
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    try:
        font = ImageFont.truetype(font_path, 28)
    except OSError:
        font = ImageFont.load_default()
    label = "CHELSEAFEEDS"
    bbox = draw.textbbox((0, 0), label, font=font)
    pad_x, pad_y = 22, 13
    x, y = 24, image.height - (bbox[3] - bbox[1]) - 2 * pad_y - 24
    draw.rounded_rectangle((x, y, x + bbox[2] - bbox[0] + 2 * pad_x, y + bbox[3] - bbox[1] + 2 * pad_y),
                           radius=8, fill=(7, 20, 38, 220), outline=(212, 175, 55, 255), width=2)
    draw.text((x + pad_x, y + pad_y - bbox[1]), label, font=font, fill=(255, 255, 255, 255),
              stroke_width=0)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, "JPEG", quality=88, optimize=True, progressive=True)
    return output_path.stat().st_size

def make_image_assets(draft, story, images_dir, session, remaining):
    if IMAGE_PROVIDER != "cloudflare":
        return [], "unsupported_provider", remaining
    if not (CF_ACCOUNT_ID and CF_API_TOKEN):
        return [], "not_configured", remaining
    if remaining <= 0:
        return [], "per_run_image_limit_reached", remaining
    base = slug(draft.get("title") or story.get("title"))
    prompts = [
        ("thumbnail", str(draft.get("image_prompt", "")) +
         ". Wide 16:9 premium editorial illustration for a Chelsea FC news article, no words, no logos, no real identifiable players."),
        ("inline-01", "A second, distinct supporting editorial illustration for this story: " +
         str(draft.get("image_prompt", "")) +
         ". Wide 16:9 composition, complementary visual angle, no words, no logos, no real identifiable players.")
    ]
    assets, failures = [], []
    for role, prompt in prompts:
        if remaining <= 0:
            break
        filename = "chelseafeeds-" + base + "-" + role + ".jpg"
        path = images_dir / filename
        # Count every provider attempt, including failures, so the run cap is real.
        remaining -= 1
        try:
            raw = cloudflare_image(prompt, session)
            size = brand_image(raw, path)
            assets.append({"role": role, "filename": filename, "local_path": "images/" + filename,
                           "alt_text": str(draft.get("image_alt_text", ""))[:250],
                           "brand_overlay": "CHELSEAFEEDS bottom-left", "branded": True,
                           "width": 1200, "height": 675, "bytes": size})
        except Exception as exc:
            failures.append(role + ": " + type(exc).__name__ + ": " + str(exc)[:180])
            logging.warning("Image generation failed for %s/%s: %s", story.get("event_id"), role, exc)
    status = "generated" if len(assets) == 2 else ("partial" if assets else "failed")
    if failures:
        status += "; " + " | ".join(failures)
    return assets, status, remaining

def insert_inline_placeholder(html, assets):
    inline = next((x for x in assets if x["role"].startswith("inline")), None)
    if not inline:
        return html
    tag = ('<figure class="separator" style="clear:both;text-align:center">'
           '<img src="IMAGE_UPLOAD_REQUIRED/' + inline["filename"] + '" alt="' +
           inline["alt_text"].replace('"', "&quot;") +
           '" style="max-width:100%;height:auto" loading="lazy"/></figure>')
    match = re.search(r"</p\s*>", html, re.I)
    if match:
        return html[:match.end()] + tag + html[match.end():]
    return tag + html

def process(input_path, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir = output_dir / "images"
    data = json.loads(input_path.read_text(encoding="utf-8"))
    if data.get("collector_version") != "4.0":
        raise ValueError("Expected Collector v4.0 input; refusing unexpected artifact")
    stories = data.get("stories", [])
    session = requests.Session()
    session.headers.update(HEADERS)
    drafts, results, counters = [], [], Counter()
    images_remaining = MAX_IMAGES_PER_RUN
    for story in stories:
        fetched = [fetch_source(url, session) for url in story.get("source_urls", [])[:5]]
        assessment = evidence_assessment(story, fetched)
        record = {"event_id": story.get("event_id"), "headline": story.get("title"),
            "story_type": story.get("story_type"), "team_scope": story.get("team_scope"),
            "source_checks": [{k:v for k,v in x.items() if k != "excerpt"} for x in fetched],
            "evidence_assessment": assessment, "verification_status": "evidence_triaged_not_fact_verified",
            "claims_verified": False, "article_generated": False, "published_to_blogger": False,
            "published_to_x": False}
        counters[assessment["evidence_tier"]] += 1
        if not assessment["eligible_for_draft"]:
            record["processing_status"] = "held_insufficient_accessible_evidence"
        elif not AI_API_KEY:
            record["processing_status"] = "draft_waiting_for_ai_provider_api_key"
        elif len(drafts) >= MAX_DRAFTS:
            record["processing_status"] = "held_run_draft_limit"
        else:
            try:
                draft = generate_draft(story, fetched, AI_API_KEY, session)
                quality = quality_gate(draft, fetched, story)
                record["quality_gate"] = quality
                if not quality["passed"]:
                    record["processing_status"] = "rejected_quality_gate"
                    record["quality_failures"] = quality["failed_checks"]
                else:
                    assets, image_status, images_remaining = make_image_assets(
                        draft, story, images_dir, session, images_remaining)
                    draft["article_html"] = insert_inline_placeholder(str(draft["article_html"]), assets)
                    draft["image_assets"] = assets
                    draft["image_generation_status"] = image_status
                    draft["featured_image"] = next((x for x in assets if x["role"] == "thumbnail"), None)
                    draft["editorial_quality"] = quality
                    draft["image_note"] = ("Image is an AI-generated editorial illustration, not documentary match photography. "
                                           "Upload local image assets to Blogger and replace IMAGE_UPLOAD_REQUIRED placeholders before publishing.")
                    drafts.append({"event_id": story.get("event_id"), "collector_headline": story.get("title"),
                        "story_type": story.get("story_type"), "team_scope": story.get("team_scope"),
                        "source_urls": story.get("source_urls", []), "evidence_assessment": assessment,
                        "verification_status": "evidence_triaged_not_fact_verified", "claims_verified": False,
                        "editorial_status": "draft_requires_editorial_review", "article_generated": True,
                        "published_to_blogger": False, "published_to_x": False, "draft": draft})
                    record["processing_status"] = "draft_created_unpublished"
                    record["article_generated"] = True
                    record["image_generation_status"] = image_status
                    record["image_asset_count"] = len(assets)
            except Exception as exc:
                record["processing_status"] = "draft_generation_failed"
                record["error"] = type(exc).__name__ + ": " + str(exc)[:350]
                logging.warning("Draft failed for %s: %s", story.get("event_id"), record["error"])
        results.append(record)
        time.sleep(0.2)
    report = {"processor_version": VERSION, "generated_at": utcnow().isoformat(),
        "collector_version": data.get("collector_version"), "input_candidate_count": len(stories),
        "evidence_tier_distribution": dict(counters), "drafts_created": len(drafts),
        "max_drafts_per_run": MAX_DRAFTS, "ai_provider": AI_PROVIDER,
        "ai_api_configured": bool(AI_API_KEY), "ai_base_url": AI_BASE_URL if AI_API_KEY else None,
        "model": MODEL if AI_API_KEY else None, "image_provider": IMAGE_PROVIDER,
        "image_model": CF_MODEL, "image_api_configured": bool(CF_ACCOUNT_ID and CF_API_TOKEN),
        "max_images_per_run": MAX_IMAGES_PER_RUN, "image_attempts_used": MAX_IMAGES_PER_RUN - images_remaining,
        "article_publishing_enabled": False, "x_publishing_enabled": False, "claims_marked_verified": 0,
        "safety_controls": [
            "Evidence accessibility and publisher counts are not factual verification.",
            "Factual stories require an accessible official source or two distinct established publishers; opinion requires an accessible attributed source.",
            "Automated quality gate checks article substance, SEO fields, headings, source links and unsafe HTML; it is not independent fact-checking.",
            "No direct quotations are requested; unsupported claims must be omitted or flagged.",
            "Generated images are editorial illustrations, not documentary match photographs.",
            "Every image receives a deterministic CHELSEAFEEDS bottom-left brand overlay using Pillow.",
            "Image-generation errors never discard a quality-passed article draft.",
            "Images are attached as workflow artifacts; upload them to Blogger and replace placeholders before publication.",
            "Cloudflare Workers AI free daily allocation is capped by the provider; this workflow limits each run to six image requests and does not retry failures.",
            "No Blogger or X credentials are read; publishing remains disabled."
        ], "results": results}
    (output_dir / "article_drafts.json").write_text(json.dumps({"processor_version": VERSION, "drafts": drafts}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "editorial_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logging.info("Processor v%s: %d candidates, %d drafts, %d image requests used", VERSION, len(stories), len(drafts), report["image_attempts_used"])

def self_test():
    story = {"story_type":"match_report"}
    one = [{"accessible":True,"domain":"bbc.co.uk","url":"https://www.bbc.co.uk/a"}]
    two = one + [{"accessible":True,"domain":"skysports.com","url":"https://www.skysports.com/b"}]
    official = [{"accessible":True,"domain":"uefa.com","url":"https://www.uefa.com/a"}]
    assert not evidence_assessment(story, one)["eligible_for_draft"]
    assert evidence_assessment(story, two)["eligible_for_draft"]
    assert evidence_assessment(story, official)["eligible_for_draft"]
    assert evidence_assessment({"story_type":"opinion_analysis"}, one)["eligible_for_draft"]
    assert not evidence_assessment({"story_type":"transfer_rumour"}, one)["eligible_for_draft"]
    assert evidence_assessment({"story_type":"transfer_rumour"}, two)["eligible_for_draft"]
    base = {"title":"Chelsea analysis headline","search_description":"Chelsea analysis " * 8,
        "focus_keyword":"Chelsea FC","excerpt":"Chelsea analysis excerpt","image_prompt":"Editorial illustration",
        "image_alt_text":"Chelsea themed illustration","article_html":"<h2>Context</h2><p>" + ("Chelsea football analysis. " * 110) +
        "</p><h2>What next</h2><p>More analysis.</p><p><a href='https://www.bbc.co.uk/a'>Source</a></p>"}
    assert quality_gate(base, one, story)["passed"]
    unsafe = dict(base, article_html=base["article_html"] + "<script>alert(1)</script>")
    assert not quality_gate(unsafe, one, story)["passed"]
    no_link = dict(base, article_html=base["article_html"].replace("<a href='https://www.bbc.co.uk/a'>Source</a>", "Source"))
    assert not quality_gate(no_link, one, story)["passed"]
    print("PASS: 9 evidence, quality and safety regression checks")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="input/news_candidates.json")
    parser.add_argument("--output", default="output")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    self_test()
    if not args.self_test:
        process(Path(args.input), Path(args.output))
