#!/usr/bin/env python3
"""ChelseaFeeds v1.5: evidence triage, article quality gate and deterministic branded thumbnails."""
from __future__ import annotations
import argparse, json, logging, os, re, time, tempfile
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from related_links import fetch_published_articles, select_related_articles, add_related_links

VERSION = "1.5"
TIMEOUT = (5, 12)  # bounded connect/read timeout for each source
SOURCE_FETCH_WORKERS = max(1, min(8, int(os.getenv('SOURCE_FETCH_WORKERS', '8'))))
AI_REQUEST_TIMEOUT = max(30, min(90, int(os.getenv('AI_REQUEST_TIMEOUT', '75'))))
AI_MAX_ATTEMPTS = max(1, min(3, int(os.getenv('AI_MAX_ATTEMPTS', '3'))))
MAX_FETCH_CHARS = 9000
MAX_DRAFTS = max(0, min(12, int(os.getenv("MAX_DRAFTS_PER_RUN", "10"))))
AI_PROVIDER = os.getenv("AI_PROVIDER", "groq").strip().lower()
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
MODEL = os.getenv("AI_MODEL", "openai/gpt-oss-120b").strip()
MAX_SOURCE_PACK = int(os.getenv("MAX_SOURCE_PACK", "6"))
MAX_SOURCE_CHARS = int(os.getenv("MAX_SOURCE_CHARS", "5000"))
THUMBNAIL_WIDTH = 1200
THUMBNAIL_HEIGHT = 675
OFFICIAL = {"chelseafc.com", "premierleague.com", "uefa.com", "thefa.com", "fifa.com", "efl.com"}
ESTABLISHED = {"bbc.co.uk", "bbc.com", "skysports.com", "theguardian.com", "reuters.com", "apnews.com", "espn.com", "independent.co.uk", "standard.co.uk", "nytimes.com", "theathletic.com", "football365.com", "thetimes.com", "telegraph.co.uk"}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ChelseaFeedsEditorialBot/1.4; +https://www.chelseafeeds.com/)"}
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

def fetch_sources(urls):
    """Fetch one story's source pages concurrently with isolated sessions."""
    urls = list(urls or [])[:8]
    if not urls:
        return []
    def worker(url):
        with requests.Session() as worker_session:
            worker_session.headers.update(HEADERS)
            return fetch_source(url, worker_session)
    workers = min(SOURCE_FETCH_WORKERS, len(urls))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(worker, urls))


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
    other_direct = [x for x in accessible if not in_domains(x["url"], OFFICIAL) and not in_domains(x["url"], ESTABLISHED)]
    other_domains = {x["domain"] for x in other_direct}
    if typ == "confirmed_transfer":
        eligible = bool(official)
        reason = "A completed transfer draft requires an accessible official club or competition announcement."
    elif typ == "transfer_rumour":
        eligible = bool(official) or bool(established) or len(other_domains) >= 2
        reason = "A rumour may be drafted from one accessible established publisher if explicitly attributed and labelled unconfirmed; otherwise two direct specialist sources are required."
    elif typ == "opinion_analysis":
        eligible = bool(accessible)
        reason = "Opinion may be drafted from one accessible source with clear attribution; factual premises remain unverified."
    else:
        eligible = bool(official) or bool(established) or len(other_domains) >= 2
        reason = "A news draft may use one accessible official or established publisher with explicit attribution; two direct specialist sources are required when neither is available."
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
        "For transfers, state clearly whether officially confirmed or merely reported; never infer fees or clauses. "
        "When only one publisher is supplied, attribute every development to that publisher and say it has not been independently corroborated. "
        "For opinion, attribute the view and distinguish its factual premises. Keep men, women and "
        "academy teams distinct. Write at least 400 substantive words, preferably 450-700, when supplied evidence supports it. "
        "The article must contain at least 400 words excluding the Sources section, headings, captions and image alt text. "
        "Use a clear introduction and at least three useful H2 headings. Never pad, repeat points or invent details to reach the minimum. "
        "If evidence cannot support 400 useful words, explain the limitation in editorial_notes; the processor will reject the draft. "
        "Do not write a Sources section or source hyperlinks; the processor adds a deterministic Sources section using accessible supplied URLs. Do not invent internal links. "
        "Return ONLY JSON keys: title, search_description, focus_keyword, labels, article_html, "
        "excerpt, image_prompt, image_alt_text, editorial_notes, unsupported_or_unverified_points. "
        "HTML must be Blogger-ready, with no scripts, iframe, form, schema or inline ads. "
        "Image prompt must request an editorial illustration, not fake documentary match photography "
        "or a photorealistic depiction of a real player. Chelsea blue #034694, navy #071426, white, gold."
    )
    req = {"model": MODEL, "response_format": {"type": "json_object"},
           "messages": [{"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]}
    response = None
    last_error = None
    for attempt in range(AI_MAX_ATTEMPTS):
        try:
            response = session.post(AI_BASE_URL + "/chat/completions",
                headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                json=req, timeout=AI_REQUEST_TIMEOUT)
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("retry-after")
                delay = min(25, int(retry_after)) if retry_after and retry_after.isdigit() else (3 * (2 ** attempt))
                logging.warning("AI provider returned HTTP %s; retrying in %ss", response.status_code, delay)
                time.sleep(delay)
                continue
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt == AI_MAX_ATTEMPTS - 1:
                raise
            time.sleep(3 * (2 ** attempt))
    if response is None or response.status_code >= 400:
        if last_error:
            raise last_error
        raise RuntimeError("AI provider retries exhausted")
    draft = json.loads(response.json()["choices"][0]["message"]["content"])
    draft["search_description"] = normalize_search_description(draft)
    required = {"title", "search_description", "focus_keyword", "labels", "article_html",
                "excerpt", "image_prompt", "image_alt_text", "editorial_notes", "unsupported_or_unverified_points"}
    missing = required - set(draft)
    if missing:
        raise ValueError("Model output missing keys: " + ", ".join(sorted(missing)))
    return draft

def add_deterministic_sources(html, fetched):
    """Add a reliable Sources section using only accessible URLs supplied by the collector."""
    soup = BeautifulSoup(str(html or ""), "html.parser")
    for heading in list(soup.find_all(["h2", "h3"])):
        if heading.get_text(" ", strip=True).casefold() == "sources":
            node = heading
            while node:
                nxt = node.find_next_sibling()
                node.decompose()
                if nxt and nxt.name in {"h2", "h3"}:
                    break
                node = nxt
    allowed = {x["url"] for x in fetched if x.get("accessible") and x.get("url")}
    for anchor in list(soup.find_all("a", href=True)):
        if anchor.get("href") not in allowed:
            anchor.unwrap()
    source_rows, seen = [], set()
    for item in fetched:
        url = item.get("url")
        if item.get("accessible") and url in allowed and url not in seen:
            seen.add(url)
            source_rows.append((host(url), url))
    if source_rows:
        section = soup.new_tag("section")
        heading = soup.new_tag("h2")
        heading.string = "Sources"
        section.append(heading)
        listing = soup.new_tag("ul")
        for publisher, url in source_rows:
            li = soup.new_tag("li")
            anchor = soup.new_tag("a", href=url)
            anchor.string = publisher
            li.append(anchor)
            listing.append(li)
        section.append(listing)
        soup.append(section)
    return str(soup)

def normalize_search_description(draft):
    """Build a factual 100-180 character Blogger description from the article itself."""
    title = re.sub(r"\\s+", " ", str(draft.get("title") or "")).strip()
    description = re.sub(r"\\s+", " ", str(draft.get("search_description") or "")).strip()
    if 100 <= len(description) <= 180:
        return description
    if len(description) > 180:
        shortened = description[:178].rsplit(" ", 1)[0].rstrip(" ,;:-")
        return shortened + "."
    soup = BeautifulSoup(str(draft.get("article_html") or ""), "html.parser")
    paragraphs = [re.sub(r"\\s+", " ", p.get_text(" ", strip=True)).strip()
                  for p in soup.find_all("p")]
    paragraphs = [p for p in paragraphs if len(p) > 35]
    factual_intro = " ".join(paragraphs[:2])
    candidate = (title + ". " + factual_intro).strip(" .")
    if len(candidate) > 180:
        candidate = candidate[:178].rsplit(" ", 1)[0].rstrip(" ,;:-") + "."
    if len(candidate) < 100:
        # Do not invent filler: keep the available factual wording and allow the quality gate to reject it.
        return candidate
    return candidate


def quality_gate(draft, fetched, story, allowed_internal_urls=None):
    html = str(draft.get("article_html", ""))
    soup = BeautifulSoup(html, "html.parser")

    # Exclude the deterministic Sources block from the substantive article word count.
    for heading in list(soup.find_all(["h2", "h3"])):
        if heading.get_text(" ", strip=True).casefold() == "sources":
            parent = heading.parent
            if parent and parent.name == "section":
                parent.decompose()
            else:
                node = heading
                while node:
                    nxt = node.find_next_sibling()
                    node.decompose()
                    if nxt and nxt.name in {"h2", "h3"}:
                        break
                    node = nxt

    original_soup = BeautifulSoup(html, "html.parser")
    # Headings, captions and image alt text are structure/metadata, not article body words.
    for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "figcaption", "img"]):
        node.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    word_count = len(text.split())
    links = {a.get("href") for a in original_soup.find_all("a", href=True)}
    allowed_urls = {x["url"] for x in fetched if x.get("accessible") and x.get("url")}
    allowed_internal_urls = set(allowed_internal_urls or [])
    title = str(draft.get("title", "")).strip()
    description = str(draft.get("search_description", "")).strip()
    checks = {
        "title_present_and_reasonable": 25 <= len(title) <= 100,
        "search_description_length": 100 <= len(description) <= 180,
        "substantive_article_body_minimum_400_words": word_count >= 400,
        "has_section_headings": len(original_soup.find_all(["h2", "h3"])) >= 2,
        "has_source_links": bool(links & allowed_urls),
        "source_links_are_supplied": links.issubset(allowed_urls | allowed_internal_urls),
        "no_unsafe_html": not bool(re.search(r"<(script|iframe|form|object|embed)\b", html, re.I)),
        "no_empty_required_seo_fields": all(str(draft.get(k, "")).strip() for k in ("focus_keyword", "excerpt", "image_prompt", "image_alt_text"))
    }
    failed = [k for k, ok in checks.items() if not ok]
    score = round(100 * sum(checks.values()) / len(checks))
    # Every check is mandatory; a high score cannot override a failed SEO, length, source or safety check.
    passed = not failed
    return {"passed": passed, "score": score, "word_count": word_count,
            "minimum_word_count": 400, "checks": checks, "failed_checks": failed,
            "note": "Automated editorial screening only; it is not independent fact-checking."}

def create_branded_thumbnail(draft, story, images_dir):
    """Create a no-API, no-external-dependency editorial thumbnail with Pillow."""
    from PIL import Image, ImageDraw, ImageFont
    width, height = THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    # Chelsea-blue to deep-navy diagonal-style gradient.
    for y in range(height):
        for x in range(width):
            t = min(1.0, (x / width) * 0.72 + (y / height) * 0.28)
            pixels[x, y] = (int(3 + 4*t), int(70 - 50*t), int(148 - 112*t))
    draw = ImageDraw.Draw(image, "RGBA")

    # Abstract stadium lights, pitch geometry and football motif; no club crest or player likeness.
    for cx, cy, radius in [(970, 175, 170), (1080, 520, 245), (770, 590, 180)]:
        draw.ellipse((cx-radius, cy-radius, cx+radius, cy+radius),
                     outline=(255,255,255,24), width=3)
    for i in range(7):
        x = 760 + i * 62
        draw.line((x, 0, x-110, 255), fill=(255,255,255,22), width=3)
    draw.rectangle((0, 520, width, 675), fill=(4, 15, 31, 105))
    draw.line((760, 560, 1190, 560), fill=(255,255,255,65), width=3)
    draw.arc((850, 485, 1170, 805), 180, 360, fill=(255,255,255,70), width=3)
    # Stylized football.
    bx, by, br = 1050, 365, 72
    draw.ellipse((bx-br,by-br,bx+br,by+br), fill=(245,247,250,235), outline=(212,175,55,255), width=5)
    draw.polygon([(bx,by-30),(bx+28,by-9),(bx+17,by+24),(bx-17,by+24),(bx-28,by-9)],
                 fill=(7,20,38,235))
    for dx,dy in [(-42,-34),(43,-31),(-46,39),(45,38)]:
        draw.line((bx,by,bx+dx,by+dy), fill=(7,20,38,180), width=5)

    font_bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font_regular = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    try:
        brand_font = ImageFont.truetype(font_bold, 27)
        title_font = ImageFont.truetype(font_bold, 54)
        kicker_font = ImageFont.truetype(font_regular, 22)
    except OSError:
        brand_font = title_font = kicker_font = ImageFont.load_default()

    draw.rounded_rectangle((55, 48, 420, 101), radius=12,
                           fill=(7,20,38,225), outline=(212,175,55,255), width=2)
    draw.text((76, 59), "CHELSEAFEEDS", font=brand_font, fill=(255,255,255,255))
    story_type = str(story.get("story_type", "chelsea_news")).replace("_", " ").upper()
    draw.text((60, 145), story_type[:34], font=kicker_font, fill=(235,196,92,255))

    title = re.sub(r"\s+", " ", str(draft.get("title") or story.get("title") or "Chelsea FC latest news")).strip()
    words, lines, current = title.split(), [], ""
    max_width = 760
    for word in words:
        candidate = (current + " " + word).strip()
        if draw.textbbox((0,0), candidate, font=title_font)[2] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
        if len(lines) >= 3:
            break
    if current and len(lines) < 4:
        lines.append(current)
    if len(lines) > 4:
        lines = lines[:4]
        lines[-1] = lines[-1].rstrip(" .") + "…"
    y = 205
    for line in lines:
        draw.text((58, y), line, font=title_font, fill=(255,255,255,255),
                  stroke_width=2, stroke_fill=(4,15,31,150))
        y += 68
    draw.rectangle((58, 505, 245, 512), fill=(212,175,55,255))

    base = slug(draft.get("title") or story.get("title"))
    filename = "chelseafeeds-" + base + "-thumbnail.jpg"
    path = images_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "JPEG", quality=90, optimize=True, progressive=True)
    return {"role":"thumbnail", "filename":filename, "local_path":"images/"+filename,
            "alt_text":str(draft.get("image_alt_text") or title)[:250],
            "brand_overlay":"CHELSEAFEEDS top-left", "branded":True,
            "width":width, "height":height, "bytes":path.stat().st_size}

def make_image_assets(draft, story, images_dir):
    asset = create_branded_thumbnail(draft, story, images_dir)
    return [asset], "generated_branded_template"

def process(input_path, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir = output_dir / "images"
    data = json.loads(input_path.read_text(encoding="utf-8"))
    if data.get("collector_version") != "4.0":
        raise ValueError("Expected Collector v4.0 input; refusing unexpected artifact")
    stories = data.get("stories", [])
    session = requests.Session()
    session.headers.update(HEADERS)
    published_articles = fetch_published_articles(session)
    logging.info("Loaded %d published ChelseaFeeds articles for internal-link matching", len(published_articles))
    drafts, results, counters = [], [], Counter()
    for story_index, story in enumerate(stories, start=1):
        logging.info("Candidate %d/%d: %s", story_index, len(stories), str(story.get("title") or story.get("event_id") or "untitled")[:140])
        source_urls = story.get("source_urls", [])[:8]
        fetched = fetch_sources(source_urls)
        logging.info("Evidence fetch complete for %s: %d/%d source pages accessible",
                     story.get("event_id"), sum(1 for item in fetched if item.get("accessible")), len(fetched))
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
                logging.info("Starting AI article generation for %s (timeout=%ss, max attempts=%s)",
                             story.get("event_id"), AI_REQUEST_TIMEOUT, AI_MAX_ATTEMPTS)
                draft = generate_draft(story, fetched, AI_API_KEY, session)
                logging.info("AI article response received for %s", story.get("event_id"))
                draft["article_html"] = add_deterministic_sources(draft["article_html"], fetched)
                related = select_related_articles(draft, story, published_articles)
                draft["article_html"] = add_related_links(draft["article_html"], related)
                draft["related_internal_links"] = [{"title": x["title"], "url": x["url"], "score": x["score"]} for x in related]
                quality = quality_gate(draft, fetched, story, {x["url"] for x in related})
                record["quality_gate"] = quality
                if not quality["passed"]:
                    record["processing_status"] = "rejected_quality_gate"
                    record["quality_failures"] = quality["failed_checks"]
                else:
                    assets, image_status = make_image_assets(draft, story, images_dir)
                    draft["image_assets"] = assets
                    draft["image_generation_status"] = image_status
                    draft["featured_image"] = next((x for x in assets if x["role"] == "thumbnail"), None)
                    draft["editorial_quality"] = quality
                    draft["image_note"] = ("Thumbnail is a deterministic ChelseaFeeds-branded graphic created locally; it uses no AI image API or external image service. "
                                           "Upload the thumbnail to Blogger and use it as the post featured image.")
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
    logging.info("Evidence and article processing complete: %d drafts from %d candidates", len(drafts), len(stories))
    blogger_posts = []
    for item in drafts:
        d = item["draft"]
        featured = item.get("draft", {}).get("featured_image") or {}
        post_slug = slug(d.get("title") or item.get("collector_headline"))
        blogger_posts.append({
            "event_id": item.get("event_id"),
            "title": d.get("title"),
            "content_html": d.get("article_html"),
            "search_description": d.get("search_description"),
            "labels": d.get("labels") if isinstance(d.get("labels"), list) else [],
            "custom_permalink_slug": post_slug,
            "focus_keyword": d.get("focus_keyword"),
            "excerpt": d.get("excerpt"),
            "featured_image": featured,
            "image_alt_text": d.get("image_alt_text"),
            "source_urls": item.get("source_urls", []),
            "editorial_status": "draft_requires_editorial_review",
            "claims_verified": False,
            "publishing_enabled": False
        })
    (output_dir / "blogger_ready_posts.json").write_text(
        json.dumps({"format_version":"1.0","generated_at":utcnow().isoformat(),
                    "instructions":"Copy content_html into Blogger HTML view. Upload featured_image separately and set it as the post thumbnail. Review claims and sources before publishing.",
                    "posts":blogger_posts}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {"processor_version": VERSION, "generated_at": utcnow().isoformat(),
        "collector_version": data.get("collector_version"), "input_candidate_count": len(stories), "blogger_ready_export_count": len(blogger_posts),
        "evidence_tier_distribution": dict(counters), "drafts_created": len(drafts),
        "max_drafts_per_run": MAX_DRAFTS, "ai_provider": AI_PROVIDER,
        "ai_api_configured": bool(AI_API_KEY), "ai_base_url": AI_BASE_URL if AI_API_KEY else None,
        "model": MODEL if AI_API_KEY else None,
        "thumbnail_method": "local_pillow_branded_template", "external_image_api_required": False,
        "article_publishing_enabled": False, "x_publishing_enabled": False, "claims_marked_verified": 0,
        "safety_controls": [
            "Evidence accessibility and publisher counts are not factual verification.",
            "News may use one accessible official or established publisher with explicit attribution; confirmed transfers require official confirmation; rumours remain labelled and attributed.",
            "Automated quality gate requires at least 400 substantive article words excluding the Sources block; all SEO, heading, source-link and HTML safety checks are mandatory.",
        "A score cannot override any failed mandatory check; short drafts and invalid SEO metadata are rejected.",
            "No direct quotations are requested; unsupported claims must be omitted or flagged.",
            "Featured thumbnails are generated locally with Pillow; no image-generation API, API key or paid image service is required.",
            "Thumbnails use ChelseaFeeds colours, article headlines and abstract football/stadium graphics; no club crest or real-player likeness is fabricated.",
            "Thumbnails are attached as workflow artifacts and must be uploaded to Blogger before publication.",
            "No Blogger or X credentials are read; publishing remains disabled."
        ], "results": results}
    (output_dir / "article_drafts.json").write_text(json.dumps({"processor_version": VERSION, "drafts": drafts}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "editorial_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logging.info("Processor v%s: %d candidates, %d drafts; local branded thumbnails require no external image API", VERSION, len(stories), len(drafts))

def self_test():
    story = {"story_type":"match_report"}
    one = [{"accessible":True,"domain":"bbc.co.uk","url":"https://www.bbc.co.uk/a"}]
    two = one + [{"accessible":True,"domain":"skysports.com","url":"https://www.skysports.com/b"}]
    official = [{"accessible":True,"domain":"uefa.com","url":"https://www.uefa.com/a"}]
    assert evidence_assessment(story, one)["eligible_for_draft"]
    assert evidence_assessment(story, two)["eligible_for_draft"]
    assert evidence_assessment(story, official)["eligible_for_draft"]
    assert evidence_assessment({"story_type":"opinion_analysis"}, one)["eligible_for_draft"]
    assert evidence_assessment({"story_type":"transfer_rumour"}, one)["eligible_for_draft"]
    assert evidence_assessment({"story_type":"transfer_rumour"}, two)["eligible_for_draft"]

    def make_draft(article_words=500):
        # The inline source anchor contributes two words; headings are excluded from the count.
        paragraph_words = max(0, article_words - 2)
        body = " ".join(["Chelsea"] * paragraph_words)
        return {
            "title":"Chelsea analysis: the key issues supporters should understand",
            "search_description":"Chelsea FC analysis examines the latest evidence, tactical questions and what supporters should watch next this season.",
            "focus_keyword":"Chelsea FC analysis",
            "excerpt":"An evidence-led Chelsea FC analysis.",
            "image_prompt":"Editorial illustration of a football stadium in Chelsea colours.",
            "image_alt_text":"Editorial illustration in Chelsea blue.",
            "labels":["Latest News","Opinion & Analysis"],
            "article_html":"<h2>Context</h2><p>" + body + "</p><h2>Next steps</h2><p><a href='https://www.bbc.co.uk/a'>BBC source</a></p>"
        }

    # SEO description fallback uses the title and article opening, not invented facts.
    short_meta = make_draft(400)
    short_meta["search_description"] = "Chelsea news"
    short_meta["article_html"] = "<h2>Context</h2><p>Chelsea secured an important result after a disciplined performance across the match, with the supplied report outlining the key moments and tactical details.</p><h2>Analysis</h2><p>Supporters will be watching the next fixture closely.</p><p><a href='https://www.bbc.co.uk/a'>BBC source</a></p>"
    normalized = normalize_search_description(short_meta)
    assert 100 <= len(normalized) <= 180, normalized
    assert "Chelsea analysis: the key issues supporters should understand" in normalized
    too_long_meta = make_draft(400)
    too_long_meta["search_description"] = "Chelsea " + ("supporters " * 30)
    assert len(normalize_search_description(too_long_meta)) <= 180

    valid = make_draft(400)
    assert quality_gate(valid, one, story)["passed"], quality_gate(valid, one, story)
    short = make_draft(399)
    short_result = quality_gate(short, one, story)
    assert not short_result["passed"] and short_result["word_count"] == 399
    assert "substantive_article_body_minimum_400_words" in short_result["failed_checks"]

    bad_title = dict(valid, title="Too short")
    assert not quality_gate(bad_title, one, story)["passed"]
    assert "title_present_and_reasonable" in quality_gate(bad_title, one, story)["failed_checks"]

    bad_description = dict(valid, search_description="Too short")
    assert not quality_gate(bad_description, one, story)["passed"]
    assert "search_description_length" in quality_gate(bad_description, one, story)["failed_checks"]

    unsafe = dict(valid, article_html=valid["article_html"] + "<script>alert(1)</script>")
    assert not quality_gate(unsafe, one, story)["passed"]
    no_link = dict(valid, article_html=valid["article_html"].replace("<a href='https://www.bbc.co.uk/a'>BBC source</a>", "BBC source"))
    assert not quality_gate(no_link, one, story)["passed"]
    bad_link = dict(valid, article_html=valid["article_html"] + "<p><a href='https://bad.example/x'>Bad source</a></p>")
    assert not quality_gate(bad_link, one, story)["passed"]

    source_html = add_deterministic_sources("<h2>Analysis</h2><p><a href='https://bad.example/x'>bad link</a></p>", one)
    source_soup = BeautifulSoup(source_html, "html.parser")
    assert source_soup.find("h2", string="Sources") is not None
    assert source_soup.find("a", href="https://www.bbc.co.uk/a") is not None
    assert source_soup.find("a", href="https://bad.example/x") is None
    duplicate = add_deterministic_sources("<h2>Analysis</h2><p>Text</p><h2>Sources</h2><p>Fake</p>", one)
    duplicate_soup = BeautifulSoup(duplicate, "html.parser")
    assert len([h for h in duplicate_soup.find_all(["h2", "h3"]) if h.get_text(strip=True) == "Sources"]) == 1

    with tempfile.TemporaryDirectory() as temp_dir:
        asset = create_branded_thumbnail(valid, {"title": valid["title"], "story_type":"match_report"}, Path(temp_dir))
        assert asset["width"] == 1200 and asset["height"] == 675
        assert asset["role"] == "thumbnail" and asset["branded"] is True
        assert (Path(temp_dir) / asset["filename"]).is_file()
    with_sources = dict(valid, article_html=valid["article_html"] + "<section><h2>Sources</h2><ul><li><a href='https://www.bbc.co.uk/a'>BBC</a></li></ul></section>")
    assert quality_gate(with_sources, one, story)["word_count"] == 400
    print("PASS: evidence, 500-word, SEO, source-link, source-exclusion, safety and local-thumbnail regression checks")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="input/news_candidates.json")
    parser.add_argument("--output", default="output")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    self_test()
    if not args.self_test:
        process(Path(args.input), Path(args.output))
