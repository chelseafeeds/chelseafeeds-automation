#!/usr/bin/env python3
"""ChelseaFeeds evidence triage and article-draft processor.

This stage checks source accessibility and evidence quality, then creates
unpublished editorial drafts when the configured evidence threshold is met.
It never marks claims as verified and never publishes to Blogger or X.
"""
from __future__ import annotations
import argparse, json, logging, os, re, time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

VERSION = "1.0"
TIMEOUT = 20
MAX_FETCH_CHARS = 9000
MAX_DRAFTS = int(os.getenv("MAX_DRAFTS_PER_RUN", "6"))
MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
OFFICIAL = {"chelseafc.com", "premierleague.com", "uefa.com", "thefa.com", "fifa.com", "efl.com"}
ESTABLISHED = {"bbc.co.uk", "bbc.com", "skysports.com", "theguardian.com", "reuters.com", "apnews.com", "espn.com", "independent.co.uk", "standard.co.uk", "nytimes.com", "theathletic.com", "football365.com", "thetimes.com", "telegraph.co.uk"}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ChelseaFeedsEditorialBot/1.0; +https://www.chelseafeeds.com/)"}
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

def utcnow():
    return datetime.now(timezone.utc)

def host(url):
    return (urlparse(url or "").hostname or "").lower().removeprefix("www.")

def in_domains(url, domains):
    d = host(url)
    return any(d == x or d.endswith("." + x) for x in domains)

def clean_text(html):
    soup = BeautifulSoup(html or "", "html.parser")
    for node in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        node.decompose()
    title = ""
    if soup.title:
        title = soup.title.get_text(" ", strip=True)
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
    # These are editorial eligibility rules, not proof that every claim is true.
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
        reason = "Transfer coverage requires official evidence or two accessible established publishers; rumours must remain explicitly unconfirmed."
    elif typ in {"match_report", "match_reaction", "match_preview", "injury_update", "team_news", "club_news"}:
        eligible = bool(official) or len({x["domain"] for x in established}) >= 2
        reason = "Factual sports coverage requires an accessible official source or two distinct established publisher domains."
    else:
        eligible = bool(official) or len({x["domain"] for x in established}) >= 2
        reason = "General factual coverage requires an accessible official source or two distinct established publisher domains."
    # A single syndicated story repeated across URLs from one publisher is not corroboration.
    return {
        "evidence_tier": tier,
        "accessible_source_count": len(accessible),
        "accessible_publisher_domains": accessible_domains,
        "official_source_count": len(official),
        "established_source_domains": sorted({x["domain"] for x in established}),
        "eligible_for_draft": eligible,
        "claims_verified": False,
        "reason": reason,
        "warning": "Accessibility and publisher count do not prove independent sourcing or factual accuracy."
    }

def build_prompt(story, evidence):
    source_pack = []
    for item in evidence:
        if item["accessible"]:
            source_pack.append({
                "url": item["url"], "domain": item["domain"],
                "page_title": item["page_title"], "description": item["description"],
                "source_excerpt": item["excerpt"][:6500]
            })
    return {
        "story": {
            "headline": story.get("title"),
            "story_type": story.get("story_type"),
            "team_scope": story.get("team_scope"),
            "published": story.get("published"),
            "collector_summary": story.get("best_summary", ""),
            "original_source_links": story.get("source_urls", [])
        },
        "accessible_source_material": source_pack
    }

def generate_draft(story, evidence, api_key, session):
    payload = build_prompt(story, evidence)
    system = (
        "You are a careful Chelsea FC sports journalist and SEO editor for ChelseaFeeds. "
        "Create an original, readable article using ONLY the supplied source material. "
        "Do not invent facts, quotes, dates, scores, statistics, injuries, transfer fees, "
        "contract details, line-ups, venues or motives. If a detail is not supported, omit "
        "it or explicitly say it has not been confirmed. Do not copy source wording. "
        "Do not imply two publishers are independent if that is unknown. "
        "For transfer_rumour, label the story as a report/rumour and attribute it to its "
        "source; never present interest, talks, bids or fees as confirmed. "
        "For opinion_analysis, attribute opinions and keep them distinct from facts. "
        "Keep the correct team scope (men, women or academy) clear. "
        "Produce a useful 800-1200 word draft when evidence supports it; shorter is better "
        "than padding or speculation. Include source links in a short Sources section. "
        "Use natural language, useful subheadings, a concise intro and original analysis. "
        "Return ONLY valid JSON with keys: title, search_description, focus_keyword, labels "
        "(array), article_html, excerpt, image_prompt, image_alt_text, editorial_notes "
        "(array), unsupported_or_unverified_points (array). "
        "article_html must be Blogger-ready HTML using h2, h3, p, ul/li where useful. "
        "Do not include schema markup, scripts, iframe, inline ads or fabricated internal links. "
        "Never state that the article or its claims are verified."
    )
    req = {
        "model": MODEL,
        "reasoning_effort": "low",
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
        ]
    }
    response = session.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        json=req, timeout=90
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    draft = json.loads(content)
    required = {"title", "search_description", "focus_keyword", "labels", "article_html",
                "excerpt", "image_prompt", "image_alt_text", "editorial_notes",
                "unsupported_or_unverified_points"}
    missing = required - set(draft)
    if missing:
        raise ValueError("Model output missing keys: " + ", ".join(sorted(missing)))
    html = str(draft["article_html"])
    if len(html) < 500:
        raise ValueError("Generated article body is unexpectedly short")
    if re.search(r"<(script|iframe|form)\b", html, re.I):
        raise ValueError("Unsafe HTML element detected in generated article")
    return draft

def process(input_path, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    data = json.loads(input_path.read_text(encoding="utf-8"))
    if data.get("collector_version") != "4.0":
        raise ValueError("Expected Collector v4.0 input; refusing unexpected artifact")
    stories = data.get("stories", [])
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    session = requests.Session()
    session.headers.update(HEADERS)
    drafts, results = [], []
    counters = Counter()
    for story in stories:
        fetched = [fetch_source(url, session) for url in story.get("source_urls", [])[:5]]
        assessment = evidence_assessment(story, fetched)
        record = {
            "event_id": story.get("event_id"),
            "headline": story.get("title"),
            "story_type": story.get("story_type"),
            "team_scope": story.get("team_scope"),
            "source_checks": [{k:v for k,v in x.items() if k != "excerpt"} for x in fetched],
            "evidence_assessment": assessment,
            "verification_status": "evidence_triaged_not_fact_verified",
            "claims_verified": False,
            "article_generated": False,
            "published_to_blogger": False,
            "published_to_x": False
        }
        counters[assessment["evidence_tier"]] += 1
        if not assessment["eligible_for_draft"]:
            record["processing_status"] = "held_insufficient_accessible_evidence"
            results.append(record)
            continue
        if not api_key:
            record["processing_status"] = "draft_waiting_for_openai_api_key"
            results.append(record)
            continue
        if len(drafts) >= MAX_DRAFTS:
            record["processing_status"] = "held_run_draft_limit"
            results.append(record)
            continue
        try:
            draft = generate_draft(story, fetched, api_key, session)
            draft_record = {
                "event_id": story.get("event_id"),
                "collector_headline": story.get("title"),
                "story_type": story.get("story_type"),
                "team_scope": story.get("team_scope"),
                "source_urls": story.get("source_urls", []),
                "evidence_assessment": assessment,
                "verification_status": "evidence_triaged_not_fact_verified",
                "claims_verified": False,
                "editorial_status": "draft_requires_editorial_review",
                "article_generated": True,
                "published_to_blogger": False,
                "published_to_x": False,
                "draft": draft
            }
            drafts.append(draft_record)
            record["processing_status"] = "draft_created_unpublished"
            record["article_generated"] = True
        except Exception as exc:
            record["processing_status"] = "draft_generation_failed"
            record["error"] = type(exc).__name__ + ": " + str(exc)[:350]
            logging.warning("Draft failed for %s: %s", story.get("event_id"), record["error"])
        results.append(record)
        time.sleep(0.2)
    report = {
        "processor_version": VERSION,
        "generated_at": utcnow().isoformat(),
        "collector_version": data.get("collector_version"),
        "input_candidate_count": len(stories),
        "evidence_tier_distribution": dict(counters),
        "drafts_created": len(drafts),
        "max_drafts_per_run": MAX_DRAFTS,
        "openai_api_configured": bool(api_key),
        "model": MODEL if api_key else None,
        "article_publishing_enabled": False,
        "x_publishing_enabled": False,
        "claims_marked_verified": 0,
        "safety_controls": [
            "Source accessibility and publisher counts are evidence triage, not factual verification.",
            "Multiple URLs from one publisher never count as multiple independent publishers.",
            "Factual drafts require an accessible official source or two distinct established publisher domains.",
            "Opinion drafts require an accessible source and must attribute opinions.",
            "Transfer rumours remain explicitly labelled and unconfirmed unless an official announcement supports completion.",
            "Every generated draft remains unpublished and requires editorial review.",
            "No Blogger or X API credentials are read or used by this processor."
        ],
        "results": results
    }
    (output_dir / "article_drafts.json").write_text(json.dumps({"processor_version": VERSION, "drafts": drafts}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "editorial_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logging.info("Processor v%s: %d candidates, %d eligible drafts, OpenAI configured=%s", VERSION, len(stories), len(drafts), bool(api_key))

def self_test():
    story = {"story_type":"match_report"}
    one = [{"accessible":True,"domain":"bbc.co.uk","url":"https://www.bbc.co.uk/a"}]
    two = one + [{"accessible":True,"domain":"skysports.com","url":"https://www.skysports.com/b"}]
    official = [{"accessible":True,"domain":"uefa.com","url":"https://www.uefa.com/a"}]
    assert evidence_assessment(story, one)["eligible_for_draft"] is False
    assert evidence_assessment(story, two)["eligible_for_draft"] is True
    assert evidence_assessment(story, official)["eligible_for_draft"] is True
    assert evidence_assessment({"story_type":"opinion_analysis"}, one)["eligible_for_draft"] is True
    assert evidence_assessment({"story_type":"transfer_rumour"}, one)["eligible_for_draft"] is False
    assert evidence_assessment({"story_type":"transfer_rumour"}, two)["eligible_for_draft"] is True
    print("PASS: 6 evidence-gate regression checks")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="input/news_candidates.json")
    parser.add_argument("--output", default="output")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    self_test()
    if not args.self_test:
        process(Path(args.input), Path(args.output))
