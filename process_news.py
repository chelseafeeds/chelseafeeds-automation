#!/usr/bin/env python3
"""ChelseaFeeds v1.6: evidence triage, article quality gate and original-source image handoff."""
from __future__ import annotations
import argparse, json, logging, os, re, time, tempfile
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from related_links import fetch_published_articles, select_related_articles, add_related_links

VERSION = "1.7"
TIMEOUT = (5, 12)  # bounded connect/read timeout for each source
SOURCE_FETCH_WORKERS = max(1, min(8, int(os.getenv('SOURCE_FETCH_WORKERS', '8'))))
AI_REQUEST_TIMEOUT = max(30, min(90, int(os.getenv('AI_REQUEST_TIMEOUT', '75'))))
AI_MAX_ATTEMPTS = max(1, min(3, int(os.getenv('AI_MAX_ATTEMPTS', '3'))))
MAX_FETCH_CHARS = 9000
MAX_DRAFTS = max(0, min(25, int(os.getenv("MAX_DRAFTS_PER_RUN", "25"))))
AI_PROVIDER = os.getenv("AI_PROVIDER", "groq").strip().lower()
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
MODEL = os.getenv("AI_MODEL", "openai/gpt-oss-120b").strip()
AI_FALLBACK_ENABLED = os.getenv("AI_FALLBACK_ENABLED", "true").strip().lower() == "true"
AI_FALLBACK_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
AI_FALLBACK_BASE_URL = os.getenv("AI_FALLBACK_BASE_URL", "https://api.openai.com/v1").rstrip("/")
AI_FALLBACK_MODEL = os.getenv("AI_FALLBACK_MODEL", "gpt-4.1-mini").strip()
MAX_SOURCE_PACK = max(1, min(8, int(os.getenv("MAX_SOURCE_PACK", "5"))))
MAX_SOURCE_CHARS = max(1200, min(6500, int(os.getenv("MAX_SOURCE_CHARS", "3500"))))
MAX_COMPLETION_TOKENS = max(1200, min(4000, int(os.getenv("MAX_COMPLETION_TOKENS", "2000"))))
RUN_TOKEN_BUDGET = max(0, int(os.getenv("RUN_TOKEN_BUDGET", "150000")))
AI_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "requests": 0, "providers": {}}

class AIQuotaExhausted(RuntimeError):
    """A provider has exhausted its quota or billing allowance."""

class AIProviderPoolUnavailable(AIQuotaExhausted):
    """Every configured AI provider is unavailable for the remainder of this run."""

# Once a provider fails with quota, rate-limit, authentication, or service errors,
# quarantine it for this process run. This prevents repeatedly hitting a dead provider.
AI_DISABLED_PROVIDERS = set()
THUMBNAIL_WIDTH = 1200
THUMBNAIL_HEIGHT = 675
OFFICIAL = {"chelseafc.com", "premierleague.com", "uefa.com", "thefa.com", "fifa.com", "efl.com", "englandfootball.com"}
ESTABLISHED = {"bbc.co.uk", "bbc.com", "skysports.com", "theguardian.com", "reuters.com", "apnews.com", "espn.com", "independent.co.uk", "standard.co.uk", "nytimes.com", "theathletic.com", "football365.com", "thetimes.com", "telegraph.co.uk", "ticotimes.net", "primicias.ec", "talksport.com", "footballinsider247.com", "football.london", "90min.com", "goal.com", "fourfourtwo.com", "dailymail.co.uk", "mirror.co.uk", "thesun.co.uk", "inews.co.uk", "cbssports.com"}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ChelseaFeedsEditorialBot/1.4; +https://www.chelseafeeds.com/)"}
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

def utcnow():
    return datetime.now(timezone.utc)

def host(url):
    return (urlparse(url or "").hostname or "").lower().removeprefix("www.")

def in_domains(url, domains):
    d = host(url)
    return any(d == x or d.endswith("." + x) for x in domains)

def publisher_group(domain):
    """Normalize domains owned by the same newsroom so mirrors do not count twice."""
    d = str(domain or "").strip().lower().removeprefix("www.")
    if d in {"bbc.co.uk", "bbc.com"}:
        return "bbc"
    if d in {"nytimes.com", "theathletic.com"}:
        return "nytimes"
    if d in {"mirror.co.uk", "football.london"}:
        return "reach"
    return d

def slug(value):
    value = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return (value[:70].strip("-") or "chelsea-story")

def extract_source_images(html, page_url):
    """Extract article-embedded images with image-specific, machine-readable reuse evidence.

    A page-wide licence is deliberately NOT inherited by every image on the page.
    Images without an explicit licence attached to that image/figure are retained
    as evidence only and cannot pass the publishing gate.
    """
    from urllib.parse import urljoin
    soup = BeautifulSoup(html or "", "html.parser")
    found, seen = [], set()

    def licence_for(node):
        scope = node.find_parent("figure") or node.parent or node
        evidence_nodes = [node, scope]
        for evidence in evidence_nodes:
            for key in ("data-license", "data-licence", "license", "licence", "itemprop"):
                value = (evidence.get(key) or "").strip()
                if value and ("license" in key or "licence" in key):
                    if value.startswith("https://"):
                        name, url = classify_license(value)
                        if name:
                            return name, url
                    else:
                        name, url = classify_license(value)
                        if name:
                            return name, url
        for link in scope.select('a[rel~="license"], a[href*="creativecommons.org/licenses/"], a[href*="creativecommons.org/publicdomain/"]'):
            href = urljoin(page_url, link.get("href", "").strip())
            name, url = classify_license(href)
            if name:
                return name, url
        for meta in scope.select('[itemprop="license"], meta[name*="license" i], meta[property*="license" i]'):
            value = meta.get("content") or meta.get("href") or ""
            name, url = classify_license(urljoin(page_url, value))
            if name:
                return name, url
        return "", ""

    def classify_license(value):
        from urllib.parse import urlparse
        raw = str(value or "").strip()
        low = raw.lower()
        if "creativecommons.org/licenses/by/" in low:
            match = re.search(r"/licenses/by/(2\.0|3\.0|4\.0)(?:/|$)", low)
            if match:
                return "CC BY " + match.group(1), raw
        if "creativecommons.org/publicdomain/zero/1.0" in low:
            return "CC0 1.0", raw
        if "creativecommons.org/publicdomain/mark/1.0" in low:
            return "Public Domain Mark 1.0", raw
        return "", ""

    nodes = []
    for selector in ('article figure img', 'main figure img', 'article img', 'main img'):
        nodes.extend(soup.select(selector))
    # OG/Twitter images are accepted as source candidates only if their exact
    # URL is also represented by an image/figure carrying its own licence.
    for node in nodes:
        raw = node.get("src") or node.get("data-src") or node.get("data-lazy-src") or ""
        srcset = node.get("srcset") or ""
        if not raw and srcset:
            raw = srcset.split(",")[0].strip().split(" ")[0]
        url = urljoin(page_url, raw.strip())
        if not url.startswith("https://") or url in seen:
            continue
        seen.add(url)
        figure = node.find_parent("figure")
        caption = figure.get_text(" ", strip=True)[:350] if figure else ""
        alt = (node.get("alt") or node.get("title") or node.get("aria-label") or "").strip()[:250]
        license_name, license_url = licence_for(node)
        found.append({
            "url": url, "page_url": page_url, "alt_text": alt,
            "caption": caption, "license_name": license_name,
            "license_url": license_url, "rights_explicit": bool(license_name and license_url),
            "rights_evidence_scope": "image_or_figure"
        })
        if len(found) >= 12:
            break
    return found

def clean_text(html):
    soup = BeautifulSoup(html or "", "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for selector in ['meta[property="og:title"]', 'meta[name="twitter:title"]']:
        node = soup.select_one(selector)
        if node and node.get("content"):
            title = node["content"].strip()
            break
    desc = ""
    for selector in ['meta[property="og:description"]', 'meta[name="description"]',
                    'meta[name="twitter:description"]']:
        node = soup.select_one(selector)
        if node and node.get("content"):
            desc = node["content"].strip()
            break

    # Some publishers put the full article body in JSON-LD instead of semantic HTML.
    structured_bodies = []
    def collect_article_bodies(value):
        if isinstance(value, dict):
            body = value.get("articleBody")
            if isinstance(body, str) and len(body.strip()) >= 180:
                structured_bodies.append(body)
            for child in value.values():
                collect_article_bodies(child)
        elif isinstance(value, list):
            for child in value:
                collect_article_bodies(child)
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            collect_article_bodies(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue

    # Remove boilerplate before selecting the article container.
    for node in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        node.decompose()
    for node in soup.select('[role="navigation"], [aria-hidden="true"], .cookie, .advert, .advertisement, .social-share, .related-articles'):
        node.decompose()
    article_text = ""
    selectors = ['[itemprop="articleBody"]', 'article .entry-content', '.entry-content',
                 '.article-content', '.post-content', '.article__body', '.story-body',
                 '.article-body', '.post-body', '.content__article-body', 'article', 'main']
    for selector in selectors:
        nodes = soup.select(selector)
        if nodes:
            candidate = max((node.get_text(" ", strip=True) for node in nodes), key=len, default="")
            candidate = re.sub(r"\s+", " ", candidate).strip()
            if len(candidate) > len(article_text):
                article_text = candidate
    if len(article_text) < 180 and structured_bodies:
        article_text = max(structured_bodies, key=len)
    if len(article_text) < 180:
        article_text = (soup.body or soup).get_text(" ", strip=True)
    body = re.sub(r"\s+", " ", article_text).strip()
    return title[:300], desc[:800], body[:MAX_FETCH_CHARS]
def fetch_source(url, session):
    result = {"url": url, "domain": host(url), "http_status": None, "accessible": False,
              "page_title": "", "description": "", "excerpt": "", "error": None}
    try:
        response = None
        for attempt in range(2):
            response = session.get(url, timeout=TIMEOUT, allow_redirects=True)
            if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(1.0)
                continue
            break
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
        result["source_images"] = extract_source_images(response.text, response.url)
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


def ranked_source_urls(story, limit=8):
    """Recover alternative direct publisher links and prefer the strongest distinct domains."""
    raw = list(story.get("source_urls") or [])
    raw.extend(x.get("url") for x in story.get("source_observations", []) if isinstance(x, dict))
    raw.append(story.get("representative_url"))
    unique = list(dict.fromkeys(str(url).strip() for url in raw if str(url or "").strip()))
    def rank(url):
        if in_domains(url, OFFICIAL): return 0
        if in_domains(url, ESTABLISHED): return 1
        return 2
    unique.sort(key=lambda url: (rank(url), host(url), url))
    selected, used_domains = [], set()
    # First pass guarantees source diversity; second pass fills remaining slots.
    for url in unique:
        domain = host(url)
        if domain and domain not in used_domains and len(selected) < limit:
            selected.append(url)
            used_domains.add(domain)
    for url in unique:
        if len(selected) >= limit: break
        if url not in selected: selected.append(url)
    return selected

def evidence_assessment(story, fetched):
    accessible = [x for x in fetched if x["accessible"]]
    accessible_domains = sorted({x["domain"] for x in accessible})
    official = [x for x in accessible if in_domains(x["url"], OFFICIAL)]
    established = [x for x in accessible if in_domains(x["url"], ESTABLISHED)]
    typ = story.get("story_type", "general_news")
    if official:
        tier = "official_source_accessible"
    elif len({publisher_group(x["domain"]) for x in established}) >= 2:
        tier = "multiple_established_publishers_accessible"
    elif established:
        tier = "single_established_publisher_accessible"
    elif accessible:
        tier = "other_direct_source_accessible"
    else:
        tier = "no_usable_source"
    other_direct = [x for x in accessible if not in_domains(x["url"], OFFICIAL) and not in_domains(x["url"], ESTABLISHED)]
    # Evidence score is a triage threshold, NOT a probability that a claim is true:
    # 20 points = one directly accessible publisher page; 40 = established publisher;
    # 100 = official club/competition source. No accessible page scores zero.
    evidence_score = 100 if official else 40 if established else 20 if accessible else 0
    minimum_evidence_score = 20
    if typ == "confirmed_transfer":
        eligible = bool(official)
        reason = "Confirmed transfers still require an accessible official club or competition announcement."
    elif typ == "transfer_rumour":
        eligible = evidence_score >= minimum_evidence_score
        reason = "At 20/100 triage points, one accessible direct publisher can support an explicitly attributed and clearly unconfirmed rumour; it is not independently corroborated."
    else:
        eligible = evidence_score >= minimum_evidence_score
        reason = "At 20/100 triage points, one accessible direct publisher can support a clearly attributed draft; accessibility is not proof of factual accuracy."
    established_domains = sorted({x["domain"] for x in established})
    established_groups = sorted({publisher_group(x["domain"]) for x in established})
    # The requested 20/100 threshold applies to drafting and automatic scheduling.
    # A real, readable direct source remains mandatory; confirmed transfers remain official-only.
    auto_schedule_eligible = evidence_score >= minimum_evidence_score and bool(accessible) and (typ != "confirmed_transfer" or bool(official))
    return {"evidence_tier": tier, "evidence_score": evidence_score,
            "minimum_evidence_score": minimum_evidence_score,
            "accessible_source_count": len(accessible),
            "accessible_publisher_domains": accessible_domains, "official_source_count": len(official),
            "established_source_domains": established_domains, "established_publisher_groups": established_groups,
            "eligible_for_draft": eligible, "auto_schedule_eligible": auto_schedule_eligible,
            "claims_verified": False, "reason": reason,
            "warning": "20/100 is a low evidence-triage threshold, not factual confidence. Every claim must remain attributed; unsupported claims must be omitted. Confirmed transfers require official confirmation."}

def story_source_records(story):
    """Publisher observations supplied by the collector; no article-page fetch."""
    records, seen = [], set()
    for item in story.get("source_observations", []) or []:
        if not isinstance(item, dict): continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen: continue
        seen.add(url)
        records.append({"title": str(item.get("title") or story.get("title") or "")[:300],
            "url": url, "publisher": str(item.get("publisher_name_hint") or item.get("publisher_domain") or host(url))[:120],
            "published": str(item.get("published") or "")[:80],
            "summary": str(item.get("summary") or "")[:1200],
            "source_feed": str(item.get("source_feed") or "")[:120]})
    if not records:
        urls = story.get("source_urls") or [story.get("representative_url")]
        for url in urls:
            url = str(url or "").strip()
            if url and url not in seen:
                seen.add(url)
                records.append({"title": str(story.get("title") or "")[:300], "url": url,
                    "publisher": str(story.get("representative_publisher") or host(url)),
                    "published": str(story.get("published") or ""),
                    "summary": str(story.get("best_summary") or "")[:1200], "source_feed": "collector"})
    return records[:8]


def build_prompt(story, evidence):
    pack = [{"url": x["url"], "domain": x["domain"], "page_title": x["page_title"],
             "description": x["description"], "source_excerpt": x["excerpt"][:MAX_SOURCE_CHARS]}
            for x in evidence[:MAX_SOURCE_PACK] if x.get("accessible")]
    return {"story": {"headline": story.get("title"), "story_type": story.get("story_type"),
            "team_scope": story.get("team_scope"), "published": story.get("published"),
            "collector_summary": story.get("best_summary", ""),
            "original_source_links": story.get("source_urls", [])},
            "collector_observations": story_source_records(story),
            "accessible_source_material": pack,
            "editorial_limit": "Source pages are not fetched in this mode. Treat collector headlines and summaries as unverified reported material; attribute claims to the named publisher/feed, do not invent missing details, and do not describe transfers as officially confirmed unless the supplied collector record explicitly identifies an official club announcement."}

def _request_chat_completion(payload, api_key, base_url, model, session, provider_label):
    request_payload = dict(payload)
    request_payload["model"] = model
    request_payload.setdefault("max_tokens", MAX_COMPLETION_TOKENS)
    last_error = None
    for attempt in range(AI_MAX_ATTEMPTS):
        try:
            response = session.post(base_url + "/chat/completions",
                headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                json=request_payload, timeout=AI_REQUEST_TIMEOUT)
            if response.status_code == 429 or response.status_code >= 500:
                body = response.text[:700]
                lower = body.lower()
                if response.status_code == 429 and any(term in lower for term in
                        ("insufficient_quota", "billing_not_active", "credit balance", "quota_exceeded",
                         "tokens per day", "daily token limit", "daily limit", "tpd limit")):
                    raise AIQuotaExhausted(f"AI_RATE_LIMIT_QUOTA: {provider_label} reports a quota or billing restriction: {body}")
                delay = None
                for header in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
                    value = response.headers.get(header, "").strip()
                    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s)?", value, re.I)
                    if match:
                        seconds = float(match.group(1))
                        delay = seconds / 1000 if (match.group(2) or "").lower() == "ms" else seconds
                        break
                if delay is None:
                    delay = min(60, 15 * (attempt + 1))
                delay = max(2, min(90, int(delay) + 1))
                logging.warning("%s HTTP %s; attempt %s/%s; retry delay %ss; response=%s",
                                provider_label, response.status_code, attempt + 1,
                                AI_MAX_ATTEMPTS, delay, body)
                if attempt < AI_MAX_ATTEMPTS - 1:
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"AI_HTTP_{response.status_code}: {provider_label} attempts exhausted: {body}")
            response.raise_for_status()
            data = response.json()
            usage = data.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
            AI_USAGE["prompt_tokens"] += prompt_tokens
            AI_USAGE["completion_tokens"] += completion_tokens
            AI_USAGE["total_tokens"] += total_tokens
            AI_USAGE["requests"] += 1
            provider_usage = AI_USAGE["providers"].setdefault(provider_label, {"requests": 0, "total_tokens": 0})
            provider_usage["requests"] += 1
            provider_usage["total_tokens"] += total_tokens
            logging.info("AI_USAGE provider=%s prompt_tokens=%d completion_tokens=%d total_tokens=%d run_total=%d",
                         provider_label, prompt_tokens, completion_tokens, total_tokens, AI_USAGE["total_tokens"])
            return data["choices"][0]["message"]["content"]
        except requests.RequestException as exc:
            last_error = exc
            if attempt == AI_MAX_ATTEMPTS - 1:
                raise RuntimeError(f"{provider_label} request failed: {type(exc).__name__}: {str(exc)[:300]}") from exc
            time.sleep(min(30, 5 * (attempt + 1)))
    if last_error:
        raise RuntimeError(f"{provider_label} request failed: {str(last_error)[:300]}") from last_error
    raise RuntimeError(f"{provider_label} request failed without a response")


def generate_draft(story, evidence, api_key, session, length_repair=False):
    payload = build_prompt(story, evidence)
    system = (
        "You are a careful Chelsea FC sports journalist and SEO editor. Use ONLY supplied collector headlines, timestamps and summaries. "
        "Never invent facts, quotes, dates, scores, stats, injuries, fees, contract clauses, line-ups, "
        "venues, nationalities, ages, biographies or motives. Never infer a player's nationality, age, "
        "position, current status or match record from their name or general knowledge; include such details "
        "only when explicitly stated in supplied source material. Do not add unsupported background or speculation. "
        "Do not create direct quotations; paraphrase and attribute instead. "
        "Do not copy source wording. Treat collector-only material as unverified reporting, not independently verified fact. "
        "Attribute each development to the publisher/feed named in the supplied observation. Do not claim a source page was opened or checked. "
        "For transfers, label interest, bids and negotiations as unconfirmed reports; do not call a transfer completed or officially confirmed unless the supplied collector record explicitly identifies an official club announcement. Never infer fees or clauses. "
        "For opinion, attribute the view and distinguish its factual premises. Keep men, women and "
        "academy teams distinct. Target 300 substantive words; acceptable range is 250-350 words. "
        "Count only substantive article text; exclude Sources, headings, captions and image alt text. Never exceed 350 words. "
        "Use a clear introduction and two or three useful H2 headings. Never pad, repeat points or invent details to reach the minimum. "
        "If collector summaries cannot support 250 useful words, explain the limitation in editorial_notes; the processor will reject the draft. "
        "Do not write a Sources section or source hyperlinks; the processor adds a deterministic Sources section using publisher URLs supplied by the collector. Do not invent internal links. "
        "SEO search_description is critical: write one natural, complete, factual sentence of 140-160 characters (never exceed 180), beginning with the main Chelsea topic or focus keyword. State the specific development and why it matters. Do not end mid-clause, with a dangling conjunction/preposition, or with unfinished attribution. Do not pad with generic phrases. Use only supplied facts; label rumours as unconfirmed and attribute them. "
        "Return ONLY JSON keys: title, search_description, focus_keyword, labels, article_html, "
        "excerpt, image_prompt, image_alt_text, editorial_notes, unsupported_or_unverified_points. "
        "HTML must be Blogger-ready, with no scripts, iframe, form, schema or inline ads. "
        "Image prompt must request an editorial illustration, not fake documentary match photography "
        "or a photorealistic depiction of a real player. Chelsea blue #034694, navy #071426, white, gold."
    )
    if length_repair:
        system += (
            " This is a one-time length-only quality repair. The previous version fell just below "
            "the 250-350-word range. Produce 270-330 substantive words by fully explaining distinct "
            "details already present in the supplied evidence: the speaker's points, match context, "
            "team response, named next fixture and explicitly sourced implications. Do not add new "
            "facts, unsupported tactical claims, repeated paragraphs or generic padding. If the "
            "supplied evidence cannot support that length, state the limitation in editorial_notes "
            "and do not invent material."
        )
    req = {"response_format": {"type": "json_object"},
           "messages": [{"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]}
    providers = []
    if api_key:
        providers.append((AI_PROVIDER.upper(), api_key, AI_BASE_URL, MODEL))
    if AI_FALLBACK_ENABLED and AI_FALLBACK_API_KEY:
        providers.append(("OPENAI_FALLBACK", AI_FALLBACK_API_KEY, AI_FALLBACK_BASE_URL, AI_FALLBACK_MODEL))
    if not providers:
        raise RuntimeError("No AI provider API key is configured.")

    content = None
    provider_used = None
    provider_errors = []
    for label, key, base_url, model in providers:
        if label in AI_DISABLED_PROVIDERS:
            logging.info("AI_PROVIDER_SKIPPED provider=%s reason=quarantined_for_this_run", label)
            continue
        try:
            logging.info("AI_PROVIDER_ATTEMPT provider=%s model=%s", label, model)
            content = _request_chat_completion(req, key, base_url, model, session, label)
            provider_used = label.lower()
            logging.info("AI_PROVIDER_SELECTED provider=%s", label)
            break
        except (AIQuotaExhausted, RuntimeError) as exc:
            AI_DISABLED_PROVIDERS.add(label)
            provider_errors.append(f"{label}: {str(exc)[:240]}")
            logging.error("AI_PROVIDER_QUARANTINED provider=%s reason=%s; it will not be called again in this run.",
                          label, str(exc)[:240])
            # Move immediately to the next configured provider. Never retry a
            # quota-exhausted provider for every remaining story in this batch.
            continue
    if content is None:
        details = " | ".join(provider_errors) or "all configured providers were already quarantined"
        raise AIProviderPoolUnavailable("No configured AI provider is currently usable. " + details)

    if provider_used == "openai_fallback":
        provider_used = "openai_fallback"
o_schedule_eligible"] is True
    bbc_plus_nyt = one + [{"accessible":True,"domain":"nytimes.com","url":"https://www.nytimes.com/b"}]
    assert evidence_assessment(story, bbc_plus_nyt)["auto_schedule_eligible"] is True
    assert evidence_assessment({"story_type":"opinion_analysis"}, one)["eligible_for_draft"]
    assert evidence_assessment({"story_type":"transfer_rumour"}, one)["eligible_for_draft"]
    assert evidence_assessment({"story_type":"transfer_rumour"}, two)["eligible_for_draft"]
    specialist = [{"accessible":True,"domain":"talkchelsea.net","url":"https://talkchelsea.net/story"}]
    assert evidence_assessment(story, specialist)["eligible_for_draft"]
    assert evidence_assessment(story, specialist)["auto_schedule_eligible"] is True
    assert evidence_assessment({"story_type":"transfer_rumour"}, specialist)["eligible_for_draft"]
    assert evidence_assessment({"story_type":"transfer_rumour"}, specialist)["auto_schedule_eligible"] is True
    two_established = one + [{"accessible":True,"domain":"skysports.com","url":"https://www.skysports.com/b"}]
    assert evidence_assessment(story, two_established)["auto_schedule_eligible"]
    assert evidence_assessment(story, official)["auto_schedule_eligible"]
    assert not evidence_assessment(story, [{"accessible":False,"domain":"talkchelsea.net","url":"https://talkchelsea.net/story"}])["eligible_for_draft"]
    assert evidence_assessment(story, specialist)["evidence_score"] == 20
    collector_only_story = {"title":"Chelsea update","best_summary":"A publisher reports a Chelsea development.",
        "source_urls":["https://example.com/chelsea-update"],
        "source_observations":[{"title":"Chelsea update","summary":"A publisher reports a Chelsea development.",
          "url":"https://example.com/chelsea-update","publisher_domain":"example.com","published":"2026-11-17T07:00:00+00:00"}]}
    assert story_source_records(collector_only_story)[0]["summary"] == "A publisher reports a Chelsea development."
    assert "collector_observations" in build_prompt(collector_only_story, [])
    collector_source_html = add_deterministic_sources("<h2>Context</h2><p>Story</p>", [], collector_only_story)
    assert "https://example.com/chelsea-update" in collector_source_html
    assert evidence_assessment(story, one)["evidence_score"] == 40
    assert evidence_assessment(story, official)["evidence_score"] == 100
    assert not evidence_assessment({"story_type":"confirmed_transfer"}, specialist)["eligible_for_draft"]

    fallback_draft = {"title": "Chelsea"}
    fallback_story = {"title": "Bompastor gives fitness updates on two Chelsea stars after beating Man City"}
    assert apply_source_title_fallback(fallback_draft, fallback_story)
    assert fallback_draft["title"] == fallback_story["title"]
    invalid_fallback = {"title": "Short"}
    assert not apply_source_title_fallback(invalid_fallback, {"title": "Too short"})

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
    short_meta = make_draft(300)
    short_meta["search_description"] = "Chelsea news"
    short_meta["article_html"] = "<h2>Context</h2><p>Chelsea secured an important result after a disciplined performance across the match, with the supplied report outlining the key moments and tactical details.</p><h2>Analysis</h2><p>Supporters will be watching the next fixture closely.</p><p><a href='https://www.bbc.co.uk/a'>BBC source</a></p>"
    normalized = normalize_search_description(short_meta)
    assert 100 <= len(normalized) <= 180, normalized
    assert normalized.endswith((".", "!", "?", "…")), normalized
    assert "Chelsea secured an important result" in normalized
    broken_meta = make_draft(300)
    broken_meta["search_description"] = "Chelsea have reportedly accelerated talks with transfer journalist Simon."
    broken_meta["article_html"] = "<p>Chelsea have reportedly accelerated talks to sign the forward, according to the supplied report. The club has not confirmed an agreement.</p>"
    repaired = normalize_search_description(broken_meta)
    assert repaired.startswith("Chelsea have reportedly accelerated talks to sign"), repaired
    assert 100 <= len(repaired) <= 180, repaired
    too_long_meta = make_draft(300)
    too_long_meta["search_description"] = "Chelsea " + ("supporters " * 30)
    assert len(normalize_search_description(too_long_meta)) <= 180

    valid = make_draft(300)
    assert quality_gate(valid, one, story)["passed"], quality_gate(valid, one, story)
    short = make_draft(249)
    short_result = quality_gate(short, one, story)
    assert not short_result["passed"] and short_result["word_count"] == 249
    assert should_retry_length_only(short_result)
    non_length_failure = dict(short_result, failed_checks=["substantive_article_body_minimum_250_words", "has_source_links"])
    assert not should_retry_length_only(non_length_failure)
    assert "substantive_article_body_minimum_250_words" in short_result["failed_checks"]

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

    # Regression: readable body text is whitespace-normalized and boilerplate is excluded.
    test_html = '<html><head><title>Chelsea report</title></head><body><nav>Navigation ' + ('menu ' * 50) + '</nav><article><p>' + ('Chelsea evidence ' * 40) + '</p></article><footer>Footer ' + ('links ' * 40) + '</footer></body></html>'
    source_title, source_desc, source_body = clean_text(test_html)
    assert 'Navigation' not in source_body and 'Footer' not in source_body
    assert len(source_body) >= 180 and '  ' not in source_body
    source_html = add_deterministic_sources("<h2>Analysis</h2><p><a href='https://bad.example/x'>bad link</a></p>", one)
    source_soup = BeautifulSoup(source_html, "html.parser")
    assert source_soup.find("h2", string="Sources") is not None
    assert source_soup.find("a", href="https://www.bbc.co.uk/a") is not None
    assert source_soup.find("a", href="https://bad.example/x") is None
    duplicate = add_deterministic_sources("<h2>Analysis</h2><p>Text</p><h2>Sources</h2><p>Fake</p>", one)
    duplicate_soup = BeautifulSoup(duplicate, "html.parser")
    assert len([h for h in duplicate_soup.find_all(["h2", "h3"]) if h.get_text(strip=True) == "Sources"]) == 1

    assets, image_status = make_image_assets(valid, {"title": valid["title"], "story_type":"match_report"}, Path("unused"))
    assert assets == [] and image_status == "awaiting_original_source_image_validation"
    with_sources = dict(valid, article_html=valid["article_html"] + "<section><h2>Sources</h2><ul><li><a href='https://www.bbc.co.uk/a'>BBC</a></li></ul></section>")
    assert quality_gate(with_sources, one, story)["word_count"] == 300
    # Provider failover regression: quota exhaustion must switch immediately and quarantine
    # the exhausted provider for all remaining candidates in this processing run.
    original_request = _request_chat_completion
    original_provider = AI_PROVIDER
    original_fallback_enabled = AI_FALLBACK_ENABLED
    original_fallback_key = AI_FALLBACK_API_KEY
    original_disabled = set(AI_DISABLED_PROVIDERS)
    provider_calls = []
    provider_response = json.dumps({
        "title":"Chelsea provide an update on the latest team situation",
        "search_description":"Chelsea provide an update on the latest team situation, with the reported details offering supporters a clearer picture of the club's current plans.",
        "focus_keyword":"Chelsea team news","labels":["Latest News"],
        "article_html":"<h2>Update</h2><p>Reported details.</p><h2>What it means</h2><p>Further context.</p>",
        "excerpt":"Chelsea team update","image_prompt":"Editorial Chelsea illustration",
        "image_alt_text":"Chelsea editorial illustration","editorial_notes":"",
        "unsupported_or_unverified_points":[]
    })
    def fake_provider_request(payload, key, base_url, model, session, label):
        provider_calls.append(label)
        if label == "GROQ":
            raise AIQuotaExhausted("AI_RATE_LIMIT_QUOTA: simulated Groq quota exhaustion")
        return provider_response
    try:
        _request_chat_completion = fake_provider_request
        AI_PROVIDER = "groq"
        AI_FALLBACK_ENABLED = True
        AI_FALLBACK_API_KEY = "test-openai-key"
        AI_DISABLED_PROVIDERS.clear()
        failover_one = generate_draft(collector_only_story, [], "test-groq-key", None)
        assert failover_one["ai_provider_used"] == "openai_fallback"
        assert provider_calls == ["GROQ", "OPENAI_FALLBACK"], provider_calls
        failover_two = generate_draft(collector_only_story, [], "test-groq-key", None)
        assert failover_two["ai_provider_used"] == "openai_fallback"
        assert provider_calls == ["GROQ", "OPENAI_FALLBACK", "OPENAI_FALLBACK"], provider_calls
    finally:
        _request_chat_completion = original_request
        AI_PROVIDER = original_provider
        AI_FALLBACK_ENABLED = original_fallback_enabled
        AI_FALLBACK_API_KEY = original_fallback_key
        AI_DISABLED_PROVIDERS.clear()
        AI_DISABLED_PROVIDERS.update(original_disabled)

    print("PASS: evidence, 300-word range, SEO, source extraction, source-link, safety, no-placeholder-image and AI provider failover/quarantine regression checks")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="input/news_candidates.json")
    parser.add_argument("--output", default="output")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    self_test()
    if not args.self_test:
        process(Path(args.input), Path(args.output))
