#!/usr/bin/env python3
"""ChelseaFeeds Collector v3.7 — Quality & Event Deduplication.

Collects direct publisher RSS items, classifies Chelsea coverage, merges reports
about the same event while retaining all source observations, ranks candidates,
and records observation history without marking stories as published/consumed.
It does NOT verify claims, write articles, or publish to Blogger/X.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup

VERSION = "3.7"
BASE_DIR = Path(__file__).resolve().parent
HISTORY_FILE = BASE_DIR / "seen_stories.json"
CANDIDATES_FILE = BASE_DIR / "news_candidates.json"
REPORT_FILE = BASE_DIR / "collection_report.json"
REQUEST_TIMEOUT = 18
MAX_STORIES = 40
STORY_MAX_AGE_DAYS = 7
HISTORY_DAYS = 90
MAX_HISTORY_EVENTS = 12000
EVENT_SIMILARITY_THRESHOLD = 0.38

FEEDS = [
    {"name": "BBC Sport", "url": "https://feeds.bbci.co.uk/sport/football/rss.xml"},
    {"name": "Sky Sports", "url": "https://www.skysports.com/rss/12040"},
    {"name": "The Guardian Chelsea", "url": "https://www.theguardian.com/football/chelsea/rss"},
    {"name": "TalkChelsea", "url": "https://www.talkchelsea.net/feed/"},
    {"name": "Chelsea.news", "url": "https://chelsea.news/feed"},
    {"name": "Chelsea FC Latest News", "url": "https://chelseafclatestnews.com/feed"},
    {"name": "Football365", "url": "https://www.football365.com/rss"},
]

OFFICIAL_DOMAINS = {"chelseafc.com", "premierleague.com", "uefa.com", "thefa.com"}
ESTABLISHED_DOMAINS = {
    "bbc.co.uk", "bbc.com", "skysports.com", "theguardian.com", "reuters.com",
    "apnews.com", "espn.com", "independent.co.uk", "standard.co.uk", "nytimes.com",
    "theathletic.com", "football365.com", "thetimes.com", "telegraph.co.uk",
}
AGGREGATOR_DOMAINS = {"news.google.com", "newsnow.com", "c.newsnow.com"}

IRRELEVANT_TERMS = (
    "chelsea flower show", "chelsea boots", "chelsea handler", "chelsea manning",
    "chelsea hotel", "chelsea neighbourhood", "chelsea neighborhood",
)
CHELSEA_ANCHORS = ("chelsea", "stamford bridge", "cobham")
WOMEN_TERMS = ("chelsea women", "chelsea women's", "chelsea ladies", "women's champions league", "wcl", "bompastor")
ACADEMY_TERMS = ("academy", "under-21", "under 21", "u21", "under-18", "under 18", "u18", "youth league", "cobham next gen")
TRANSFER_ACTIONS = ("transfer", "sign", "signing", "bid", "deal", "move", "loan", "contract", "interest", "target", "fee", "departure", "depart", "release clause")
TRANSFER_UNCERTAINTY = ("rumour", "rumor", "reportedly", "linked", "could sign", "may sign", "speculation", "potential", "talks", "considering", "monitoring", "interested in", "keen on", "eyeing", "eyes", "wants to sign", "set to sign")
TRANSFER_CONFIRMATION = ("officially announced", "completes", "completed", "has signed", "signs for", "joins chelsea", "extends contract", "new contract", "deal agreed", "agreement reached")
INJURY_TERMS = ("injury", "injured", "fitness", "hamstring", "knee", "ankle", "ruled out", "return to training", "sidelined", "surgery", "rehabilitation", "muscle problem")
TEAM_TERMS = ("squad", "starting xi", "line-up", "lineup", "team news", "availability", "training", "press conference", "selection", "starting eleven", "call-up", "called up")
MATCH_TERMS = ("match report", "match reaction", "premier league", "champions league", "fa cup", "carabao cup", "kick-off", "fixture", " vs ", " v ", "full-time", "half-time", "matchday", "league table", "draw", "defeat", "win", "beat", "beats", "lost", "loss", "penalty", "commentary", "goalless")
OPINION_TERMS = ("opinion", "analysis", "why", "should", "tactical", "explained", "player ratings", "power ranking", "verdict", "pundit", "claims", "argues", "believes", "view", "really worried", "failure", "problem")
STOP_WORDS = set("the a an and or but for to of in on at by with from after before as is are was were be been being this that these those his her their its our your how what why when where who which could would should may might will can has have had into over under about against says said claims claim according report reports latest news fc blues club team side player players manager boss head coach premier league football".split())

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def now_utc():
    return datetime.now(timezone.utc)


def clean_text(value):
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(str(x) for x in value)
    if isinstance(value, dict):
        value = value.get("value", "")
    return re.sub(r"\s+", " ", BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)).strip()


def domain_of(url):
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def domain_matches(domain, known):
    return any(domain == item or domain.endswith("." + item) for item in known)


def normalize_url(url):
    if not url:
        return ""
    p = urlparse(str(url).strip())
    if p.scheme.lower() not in {"http", "https"} or not p.netloc:
        return ""
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"oc", "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source"}]
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", "", urlencode(query), ""))


def publisher_type(url):
    d = domain_of(url)
    if domain_matches(d, OFFICIAL_DOMAINS):
        return "official_domain"
    if domain_matches(d, ESTABLISHED_DOMAINS):
        return "established_domain"
    if domain_matches(d, AGGREGATOR_DOMAINS):
        return "aggregator_unresolved"
    return "other_direct_domain" if d else "unknown_domain"


def parse_date(entry):
    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass
    for key in ("published_parsed", "updated_parsed"):
        raw = entry.get(key)
        if raw:
            try:
                return datetime(*raw[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def relevant(title, summary):
    t = f"{title} {summary}".lower()
    if any(x in t for x in IRRELEVANT_TERMS):
        return False
    # Chelsea must be present in title or be strongly anchored in the description.
    title_has = any(x in title.lower() for x in CHELSEA_ANCHORS)
    summary_has = any(x in summary.lower() for x in ("chelsea fc", "chelsea football club", "stamford bridge", "chelsea women", "chelsea academy"))
    return title_has or summary_has


def team_scope(title, summary):
    t = f"{title} {summary}".lower()
    if any(x in t for x in WOMEN_TERMS):
        return "chelsea_women"
    if any(x in t for x in ACADEMY_TERMS):
        return "academy_youth"
    if "chelsea" in t or "stamford bridge" in t or "cobham" in t:
        return "mens_first_team_or_club"
    return "broader_chelsea_related"


def classify_story(title, summary):
    t = f"{title} {summary}".lower()
    has_transfer_action = any(x in t for x in TRANSFER_ACTIONS)
    has_transfer_subject = bool(re.search(r"\b(player|striker|midfielder|defender|goalkeeper|forward|winger|chelsea|club)\b", t))
    confirmed = any(x in t for x in TRANSFER_CONFIRMATION)
    uncertain = any(x in t for x in TRANSFER_UNCERTAINTY)
    # Rumour requires a transfer action + transfer subject + uncertainty; pundit
    # language alone (e.g. 'pundit claims') is never sufficient.
    has_fee_signal = bool(re.search(r"[£€$]\s?\d", t)) or "transfer window" in t
    if (has_transfer_action or has_fee_signal) and has_transfer_subject and uncertain and not confirmed:
        return "transfer_rumour"
    if has_transfer_action and has_transfer_subject and confirmed:
        return "transfer_confirmed_or_official_update"
    if any(x in t for x in INJURY_TERMS):
        return "injury_update"
    if any(x in t for x in TEAM_TERMS):
        return "team_news"
    if any(x in t for x in OPINION_TERMS):
        return "opinion_analysis"
    if any(x in t for x in MATCH_TERMS):
        return "match_or_competition"
    if has_transfer_action and has_transfer_subject:
        return "transfer_development_unconfirmed"
    return "general_news"


def publisher_hint(entry):
    src = entry.get("source")
    if isinstance(src, dict):
        name = clean_text(src.get("title", ""))
        if name and name.lower() not in {"google news", "newsnow"}:
            return name[:100]
    return ""


def stable_hash(value, length=24):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def tokenize(title):
    words = re.findall(r"[a-z0-9]+", title.lower())
    return {w for w in words if w not in STOP_WORDS and len(w) > 2}


def event_similarity(a, b):
    # Events are only compared inside a 72-hour publication window.
    try:
        da = datetime.fromisoformat(a["published"])
        db = datetime.fromisoformat(b["published"])
        if abs((da - db).total_seconds()) > 72 * 3600:
            return 0.0
    except Exception:
        return 0.0
    ta, tb = a.get("title_key", ""), b.get("title_key", "")
    if a.get("team_scope") != b.get("team_scope"):
        return 0.0
    url_a = a.get("normalized_url") or a.get("url") or a.get("representative_url")
    url_b = b.get("normalized_url") or b.get("url") or b.get("representative_url")
    if url_a and url_b and normalize_url(url_a) == normalize_url(url_b):
        return 1.0
    sa, sb = tokenize(ta), tokenize(tb)
    if not sa or not sb:
        return 0.0
    jaccard = len(sa & sb) / max(1, len(sa | sb))
    sequence = SequenceMatcher(None, ta, tb).ratio()
    shared = len(sa & sb)
    # A pair of distinctive shared terms can identify a match/event even when
    # publishers phrase the headline very differently (e.g. 'Hacken 0-0 Chelsea').
    if shared >= 2 and jaccard >= 0.28:
        return max(jaccard, 0.40 + min(shared, 4) * 0.05)
    return max(jaccard, sequence * 0.85)


def build_observation(title, summary, url, feed_name, publisher_name, published):
    title, summary, url = clean_text(title), clean_text(summary), normalize_url(url)
    if not title or not url or not relevant(title, summary):
        return None
    if published is None:
        return None
    now = now_utc()
    if published > now + timedelta(hours=12) or published < now - timedelta(days=STORY_MAX_AGE_DAYS):
        return None
    domain = domain_of(url)
    ptype = publisher_type(url)
    if not domain or ptype == "aggregator_unresolved":
        return None
    scope = team_scope(title, summary)
    stype = classify_story(title, summary)
    title_key = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return {
        "title": title,
        "summary": summary[:1800],
        "url": url,
        "publisher_domain": domain,
        "publisher_name_hint": publisher_name or None,
        "source_feed": feed_name,
        "published": published.isoformat(),
        "publisher_type": ptype,
        "team_scope": scope,
        "story_type": stype,
        "title_key": title_key,
    }


def fetch_feed(config, session):
    observations = []
    report = {"source": config["name"], "url": config["url"], "status": "failed", "entries_received": 0,
              "chelsea_relevant": 0, "observations_collected": 0, "rejected_old": 0,
              "rejected_undated": 0, "rejected_irrelevant": 0, "rejected_unresolved_aggregator": 0, "error": None}
    try:
        response = session.get(config["url"], timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
        report["entries_received"] = len(parsed.entries)
        for entry in parsed.entries:
            title = clean_text(entry.get("title", ""))
            summary = clean_text(entry.get("summary", entry.get("description", "")))
            if not relevant(title, summary):
                report["rejected_irrelevant"] += 1
                continue
            report["chelsea_relevant"] += 1
            published = parse_date(entry)
            if published is None:
                report["rejected_undated"] += 1
                continue
            if published > now_utc() + timedelta(hours=12) or published < now_utc() - timedelta(days=STORY_MAX_AGE_DAYS):
                report["rejected_old"] += 1
                continue
            link = normalize_url(entry.get("link", ""))
            if domain_matches(domain_of(link), AGGREGATOR_DOMAINS):
                report["rejected_unresolved_aggregator"] += 1
                continue
            item = build_observation(title, summary, link, config["name"], publisher_hint(entry), published)
            if item:
                observations.append(item)
        report["observations_collected"] = len(observations)
        report["status"] = "success" if observations else ("empty" if parsed.entries else "empty")
        if not observations:
            report["error"] = "No eligible direct Chelsea stories in this feed"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        logging.warning("Feed failed: %s — %s", config["name"], exc)
    return observations, report


def merge_event(group, observation):
    # Keep all publisher observations; never replace a stronger source URL with
    # another publisher's URL or discard the original evidence trail.
    group["source_observations"].append(observation)
    group["source_feeds"] = sorted(set(group["source_feeds"] + [observation["source_feed"]]))
    domains = sorted(set(group["publisher_domains"] + [observation["publisher_domain"]]))
    group["publisher_domains"] = domains
    group["publisher_count"] = len(domains)
    group["observation_count"] = len(group["source_observations"])
    group["corroboration_status"] = (
        "multiple_publishers_observed_independence_not_proven" if len(domains) > 1
        else "single_publisher_observation"
    )
    if len(observation.get("summary", "")) > len(group.get("best_summary", "")):
        group["best_summary"] = observation["summary"]
    # Use the strongest source for the representative link, but retain all links.
    rank = {"official_domain": 4, "established_domain": 3, "other_direct_domain": 1}
    current = group["representative_publisher_type"]
    if rank.get(observation["publisher_type"], 0) > rank.get(current, 0):
        group["representative_url"] = observation["url"]
        group["representative_publisher"] = observation["publisher_domain"]
        group["representative_publisher_type"] = observation["publisher_type"]
        group["representative_title"] = observation["title"]
    # Prefer the most informative headline, while the representative URL stays
    # tied to its own source observation in source_observations.
    if len(observation["title"]) > len(group["title"]):
        group["title"] = observation["title"]


def cluster_events(observations):
    groups = []
    # Sort by date so near-contemporaneous reports are considered together.
    observations = sorted(observations, key=lambda x: x["published"])
    for obs in observations:
        best_group, best_score = None, 0.0
        for group in groups:
            score = event_similarity(obs, group)
            if score > best_score:
                best_group, best_score = group, score
        if best_group is None or best_score < EVENT_SIMILARITY_THRESHOLD:
            obs_id = stable_hash(obs["url"])
            groups.append({
                "event_id": obs_id,
                "title": obs["title"],
                "title_key": obs["title_key"],
                "team_scope": obs["team_scope"],
                "story_type": obs["story_type"],
                "published": obs["published"],
                "best_summary": obs["summary"],
                "representative_url": obs["url"],
                "representative_publisher": obs["publisher_domain"],
                "representative_publisher_type": obs["publisher_type"],
                "representative_title": obs["title"],
                "source_observations": [obs],
                "source_feeds": [obs["source_feed"]],
                "publisher_domains": [obs["publisher_domain"]],
                "publisher_count": 1,
                "observation_count": 1,
                "corroboration_status": "single_publisher_observation",
            })
        else:
            merge_event(best_group, obs)
            # Keep the first observation URL as the event identity. Adding a
            # second publisher must not change the ID and break repeat tracking.
            # Use the earliest date, not the last feed's date.
            best_group["published"] = min(best_group["published"], obs["published"])
            # If reports describe different types, prefer concrete factual type.
            type_order = {"transfer_confirmed_or_official_update": 8, "injury_update": 7, "team_news": 6,
                          "match_or_competition": 5, "transfer_rumour": 4, "transfer_development_unconfirmed": 3,
                          "general_news": 2, "opinion_analysis": 1}
            if type_order.get(obs["story_type"], 0) > type_order.get(best_group["story_type"], 0):
                best_group["story_type"] = obs["story_type"]
    results = []
    for g in groups:
        urls = sorted({x["url"] for x in g["source_observations"]})
        g["source_urls"] = urls
        g["source_observations"].sort(key=lambda x: (x["publisher_domain"], x["published"], x["url"]))
        g["title_key"] = re.sub(r"[^a-z0-9]+", " ", g["title"].lower()).strip()
        results.append(g)
    return results


def score_event(story):
    ptype = story["representative_publisher_type"]
    stype = story["story_type"]
    scope = story["team_scope"]
    score = 20
    score += {"official_domain": 35, "established_domain": 25, "other_direct_domain": 5}.get(ptype, 0)
    score += min(12, max(0, story["publisher_count"] - 1) * 4)
    score += {"injury_update": 12, "team_news": 10, "transfer_confirmed_or_official_update": 12,
              "match_or_competition": 9, "transfer_rumour": -5,
              "transfer_development_unconfirmed": -2, "opinion_analysis": -8,
              "general_news": 0}.get(stype, 0)
    score += {"mens_first_team_or_club": 5, "chelsea_women": 2, "academy_youth": 1,
              "broader_chelsea_related": -8}.get(scope, 0)
    summary = story.get("best_summary", "")
    if len(summary) >= 180:
        score += 5
    elif len(summary) < 45:
        score -= 5
    age_hours = max(0, (now_utc() - datetime.fromisoformat(story["published"])).total_seconds() / 3600)
    score -= min(12, int(age_hours // 12))
    score = max(0, min(100, score))
    if ptype == "official_domain":
        priority = "P1_OFFICIAL_SOURCE_REVIEW"
    elif ptype == "established_domain" and stype not in {"transfer_rumour", "opinion_analysis"}:
        priority = "P2_ESTABLISHED_REPORT_REVIEW"
    elif stype == "transfer_rumour":
        priority = "P3_TRANSFER_RUMOUR_REQUIRES_LABEL"
    elif stype == "opinion_analysis":
        priority = "P4_OPINION_LOW_PRIORITY"
    else:
        priority = "P3_SPECIALIST_OR_OTHER_SOURCE_REVIEW"
    story["importance_score"] = score
    story["priority"] = priority
    story["editorial_status"] = "pending_editorial_verification"
    story["verification_required"] = True
    story["claims_verified"] = False
    story["article_generated"] = False
    story["published_to_blogger"] = False
    story["published_to_x"] = False


def load_history():
    if not HISTORY_FILE.exists():
        return {"events": {}}
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("events"), dict):
            return data
    except Exception as exc:
        logging.warning("Could not read observation history: %s", exc)
    # Deliberately do not interpret old v3.6 queued hashes as published items.
    return {"events": {}}


def update_history(groups, history):
    events = dict(history.get("events", {}))
    now = now_utc().isoformat()
    cutoff = now_utc() - timedelta(days=HISTORY_DAYS)
    for story in groups:
        key = story["event_id"]
        prior = events.get(key, {})
        first_seen = prior.get("first_seen_at", now)
        events[key] = {
            "event_id": key,
            "title": story["title"][:240],
            "first_seen_at": first_seen,
            "last_seen_at": now,
            "last_published_at": story["published"],
            "last_observation_count": story["observation_count"],
            "publisher_domains": story["publisher_domains"],
            "status": prior.get("status", "observed_not_processed"),
        }
    pruned = {}
    for key, value in events.items():
        try:
            dt = datetime.fromisoformat(value.get("last_seen_at", "").replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                pruned[key] = value
        except Exception:
            continue
    if len(pruned) > MAX_HISTORY_EVENTS:
        ordered = sorted(pruned.items(), key=lambda x: x[1].get("last_seen_at", ""))
        pruned = dict(ordered[-MAX_HISTORY_EVENTS:])
    return {"history_version": VERSION, "updated_at": now, "events": pruned}


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    started = now_utc()
    session = requests.Session()
    session.headers.update({"User-Agent": f"Mozilla/5.0 (compatible; ChelseaFeedsBot/{VERSION}; +https://www.chelseafeeds.com/)"})
    observations, source_reports = [], []
    for config in FEEDS:
        logging.info("Collecting: %s", config["name"])
        items, report = fetch_feed(config, session)
        observations.extend(items)
        source_reports.append(report)
        time.sleep(0.15)

    events = cluster_events(observations)
    for event in events:
        score_event(event)
    priority_order = {"P1_OFFICIAL_SOURCE_REVIEW": 0, "P2_ESTABLISHED_REPORT_REVIEW": 1,
                      "P3_TRANSFER_RUMOUR_REQUIRES_LABEL": 2, "P3_SPECIALIST_OR_OTHER_SOURCE_REVIEW": 3,
                      "P4_OPINION_LOW_PRIORITY": 4}
    events.sort(key=lambda x: (priority_order.get(x["priority"], 9), -x["importance_score"], x["published"], x["title"].lower()))
    selected = events[:MAX_STORIES]
    completed = now_utc()
    history = update_history(events, load_history())
    source_status = Counter(x["status"] for x in source_reports)
    team_counts = Counter(x["team_scope"] for x in selected)
    type_counts = Counter(x["story_type"] for x in selected)
    priority_counts = Counter(x["priority"] for x in selected)
    repeat_events = sum(1 for x in selected if x["event_id"] in load_history().get("events", {}))

    candidate_data = {
        "collector_version": VERSION,
        "generated_at": completed.isoformat(),
        "selection_limit": MAX_STORIES,
        "queue_semantics": "Candidates remain pending until a later processor explicitly verifies and records publication. Collection alone never means processed or published.",
        "stories": selected,
    }
    report_data = {
        "collector_version": VERSION,
        "run_started_at": started.isoformat(),
        "run_completed_at": completed.isoformat(),
        "duration_seconds": round((completed - started).total_seconds(), 2),
        "configured_sources": len(source_reports),
        "successful_sources": source_status.get("success", 0),
        "empty_sources": source_status.get("empty", 0),
        "failed_sources": source_status.get("failed", 0),
        "raw_observations_collected": len(observations),
        "unique_events_after_event_deduplication": len(events),
        "duplicate_observations_merged": max(0, len(observations) - len(events)),
        "selected_pending_candidates": len(selected),
        "events_not_selected_due_to_limit": max(0, len(events) - len(selected)),
        "selected_events_seen_in_prior_runs": repeat_events,
        "selected_team_scope_distribution": dict(team_counts),
        "selected_story_type_distribution": dict(type_counts),
        "selected_priority_distribution": dict(priority_counts),
        "selected_events_with_multiple_publishers": sum(1 for x in selected if x["publisher_count"] > 1),
        "selected_source_link_count": sum(len(x["source_urls"]) for x in selected),
        "source_reports": source_reports,
        "excluded_sources": [
            {"source": "Google News", "status": "disabled", "reason": "Aggregator URLs were not consistently resolvable to direct publisher articles."},
            {"source": "NewsNow", "status": "disabled", "reason": "Prior extraction did not reliably produce usable direct articles."},
        ],
        "quality_controls": [
            "Stories must be Chelsea-relevant, directly linked to a publisher, dated, and no older than seven days.",
            "Chelsea Women and Academy/Youth are classified separately from men's first-team coverage.",
            "Transfer rumour classification requires both transfer-related language and uncertainty language; pundit claims alone do not trigger it.",
            "Opinion and pundit commentary receive a lower priority than factual developments.",
            "Reports about the same event are clustered and all source URLs are retained.",
            "Multiple publishers are not automatically treated as independent confirmation.",
            "Publisher reputation is a ranking signal, not proof that claims are true.",
            "All selected candidates remain pending editorial verification; no claims are marked verified.",
            "Observation history records repeated sightings only. It does not suppress candidates or mark them published.",
            "This collector does not generate articles or publish to Blogger or X.",
        ],
    }
    write_json(CANDIDATES_FILE, candidate_data)
    write_json(REPORT_FILE, report_data)
    write_json(HISTORY_FILE, history)
    logging.info("v%s finished: %d observations -> %d events -> %d pending candidates", VERSION, len(observations), len(events), len(selected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
