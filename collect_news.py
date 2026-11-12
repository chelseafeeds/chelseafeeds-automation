#!/usr/bin/env python3
"""ChelseaFeeds Collector v4.0: editorial-quality, event-aware RSS collector.

Important: collection is not fact verification. Every candidate stays pending.
Ambiguous stories are kept separate; this intentionally prefers false-split over
false-merge. No article text is generated and nothing is published.
"""
from __future__ import annotations
import hashlib, json, logging, re, sys, time
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import feedparser, requests
from bs4 import BeautifulSoup

VERSION = "4.0"
ROOT = Path(__file__).resolve().parent
HISTORY_FILE = ROOT / "seen_stories.json"
CANDIDATES_FILE = ROOT / "news_candidates.json"
REPORT_FILE = ROOT / "collection_report.json"
TIMEOUT, MAX_AGE_HOURS, MAX_STORIES, HISTORY_DAYS = 18, 36, 60, 90
MAX_OPINION_CANDIDATES = 8

FEEDS = [
    {"name":"BBC Sport","url":"https://feeds.bbci.co.uk/sport/football/rss.xml"},
    {"name":"Sky Sports","url":"https://www.skysports.com/rss/12040"},
    {"name":"The Guardian Chelsea","url":"https://www.theguardian.com/football/chelsea/rss"},
    {"name":"TalkChelsea","url":"https://www.talkchelsea.net/feed/"},
    {"name":"Chelsea.news","url":"https://chelsea.news/feed"},
    {"name":"Chelsea FC Latest News","url":"https://chelseafclatestnews.com/feed"},
    {"name":"Football365","url":"https://www.football365.com/rss"},
    {"name":"BBC Sport Women's Football","url":"https://feeds.bbci.co.uk/sport/football/womens/rss.xml"},
    {"name":"BBC Sport Premier League","url":"https://feeds.bbci.co.uk/sport/football/premier-league/rss.xml"},
    {"name":"Sky Sports Football","url":"https://www.skysports.com/rss/11095"},
    {"name":"The Guardian Football","url":"https://www.theguardian.com/football/rss"},
    {"name":"The Guardian Women's Football","url":"https://www.theguardian.com/football/womensfootball/rss"},
    {"name":"The Athletic","url":"https://www.nytimes.com/athletic/rss/news/"},
    {"name":"ESPN Soccer","url":"https://www.espn.com/espn/rss/soccer/news"},
]
OFFICIAL={"chelseafc.com","premierleague.com","uefa.com","thefa.com"}
ESTABLISHED={"bbc.co.uk","bbc.com","skysports.com","theguardian.com","reuters.com","apnews.com","espn.com","independent.co.uk","standard.co.uk","nytimes.com","theathletic.com","football365.com","thetimes.com","telegraph.co.uk"}
AGGREGATORS={"news.google.com","newsnow.com","c.newsnow.com"}
IRRELEVANT=("chelsea flower show","chelsea boots","chelsea handler","chelsea manning","chelsea hotel")
STOP=set("the a an and or but for to of in on at by with from after before as is are was were be been being this that these those his her their its our your how what why when where who which could would should may might will can has have had into over under about against says said claims claim according report reports latest news fc blues club team side player players manager boss head coach premier league football".split())
WOMEN_HINTS=("chelsea women","chelsea women's","chelsea ladies","women's champions league","women champions league","women's super league","women super league","wsl","lucy bronze","bompastor")
YOUTH_HINTS=("academy","under-21","under 21","u21","under-18","under 18","u18","youth league","cobham next gen","rio ngumoha")
INJURY=("injury","injured","fitness","hamstring","knee injury","ankle injury","ruled out","return to training","sidelined","surgery","rehabilitation","muscle problem")
OPINION=("opinion","analysis","why did","why chelsea","what .* means","verdict","pundit","claims","argues","believes","selection problem","biggest problem","failure","not improving","scary stats","might not agree","should chelsea","tactical improvement","identity still","only part")
TRANSFER_ACTION=("transfer","signing","sign","bid","deal","move","loan","contract","interest","target","fee","departure","depart","release clause","eyeing")
TRANSFER_UNCERTAIN=("rumour","rumor","reportedly","linked","could sign","may sign","speculation","potential","talks","considering","monitoring","interested in","keen on","eyeing","wants to sign","set to sign","target")
TRANSFER_CONFIRMED=("officially announced","completes","completed","has signed","signs for","joins chelsea","extends contract","new contract","deal agreed","agreement reached")
MATCH_WORDS=("match report","match reaction","premier league","champions league","fa cup","carabao cup","kick-off","fixture"," vs "," v ","full-time","half-time","matchday","league table","draw","defeat","win","beat","beats","lost","loss","penalty","commentary","goalless")

logging.basicConfig(level=logging.INFO,format="%(levelname)s: %(message)s")
def now(): return datetime.now(timezone.utc)
def textval(x):
    if isinstance(x,list): x=" ".join(map(str,x))
    if isinstance(x,dict): x=x.get("value","")
    return re.sub(r"\s+"," ",BeautifulSoup(str(x or ""),"html.parser").get_text(" ",strip=True)).strip()
def domain(url): return (urlparse(url).hostname or "").lower().removeprefix("www.")
def matches(d, names): return any(d==x or d.endswith("."+x) for x in names)
def normalize_url(url):
    p=urlparse(str(url or "").strip())
    if p.scheme.lower() not in ("http","https") or not p.netloc: return ""
    q=[(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if not k.lower().startswith("utm_") and k.lower() not in {"fbclid","gclid","mc_cid","mc_eid","ref","source","oc"}]
    return urlunparse((p.scheme.lower(),p.netloc.lower(),p.path.rstrip("/") or "/","",urlencode(q),""))
def publisher_type(url):
    d=domain(url)
    if matches(d,OFFICIAL): return "official_domain"
    if matches(d,ESTABLISHED): return "established_domain"
    if matches(d,AGGREGATORS): return "aggregator_unresolved"
    return "other_direct_domain" if d else "unknown_domain"
def parse_date(e):
    for k in ("published","updated","created"):
        if e.get(k):
            try:
                dt=parsedate_to_datetime(e[k]); dt=dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
                return dt.astimezone(timezone.utc)
            except Exception: pass
    for k in ("published_parsed","updated_parsed"):
        if e.get(k):
            try: return datetime(*e[k][:6],tzinfo=timezone.utc)
            except Exception: pass
    return None
def relevant(title,summary):
    t=(title+" "+summary).lower()
    if any(x in t for x in IRRELEVANT): return False
    return "chelsea" in title.lower() or any(x in t for x in ("chelsea fc","chelsea football club","stamford bridge","chelsea women","chelsea academy","chelsea's"))
def team_scope(title,summary,url=""):
    t=(title+" "+summary+" "+url).lower()
    # URL path hints are deliberately included: Sky commentary headlines often omit 'Women'.
    if any(x in t for x in WOMEN_HINTS) or "hacken" in t or re.search(r"women|womens|wcl|hacken",url.lower()): return "chelsea_women"
    if any(x in t for x in YOUTH_HINTS) or re.search(r"academy|youth|u21|u18|next-gen",url.lower()): return "academy_youth"
    return "mens_first_team_or_club" if "chelsea" in t or "stamford bridge" in t else "broader_chelsea_related"
def classify(title,summary):
    t=(title+" "+summary).lower()
    # FPL/club-policy stories must never be mistaken for player injury updates.
    if "fantasy premier league" in t or re.search(r"\bfpl\b",t): return "club_news"
    # Transfer signals first, but commentary/analysis must not become transfer news by incidental wording.
    action=any(x in t for x in TRANSFER_ACTION)
    confirmed=any(x in t for x in TRANSFER_CONFIRMED)
    uncertain=any(x in t for x in TRANSFER_UNCERTAIN) or (bool(re.search(r"[£€$]\s?\d",t)) and action) or (bool(re.search(r"[£€$]\s?\d",t)) and any(x in t for x in ("rumour","rumor")))
    if action and confirmed: return "confirmed_transfer"
    if (action and uncertain) or (bool(re.search(r"[£€$]\s?\d",t)) and any(x in t for x in ("rumour","rumor"))): return "transfer_rumour"
    if any(x in t for x in INJURY) and not any(x in t for x in ("stats with and without","analysis","scary stats","fantasy premier league","fpl")): return "injury_update"
    if any(re.search(p,t) for p in OPINION): return "opinion_analysis"
    # Distinguish match preview, report and reaction so different stages never merge.
    if any(x in t for x in ("preview","previews","set to face","will face","ahead of")) or re.search(r"face .+ without|before .+ match",t): return "match_preview"
    if any(x in t for x in ("not ruthless enough","reaction","reacts","responds")) or re.search(r"after .+ (draw|defeat|win)",t): return "match_reaction"
    if any(x in t for x in MATCH_WORDS) or "wcl" in t or "hacken" in t: return "match_report"
    if any(x in t for x in ("squad","starting xi","line-up","lineup","team news","availability","training","press conference","selection","starting eleven","call-up","called up")): return "team_news"
    return "general_news"
def opponent_key(title,summary,url):
    t=(title+" "+summary+" "+url).lower()
    known={"hacken":"hacken","häcken":"hacken","sunderland":"sunderland","everton":"everton","arsenal":"arsenal","barcelona":"barcelona","manchester city":"manchester_city","man city":"manchester_city","aston villa":"aston_villa","flor[a]? tallinn":"flora_tallinn"}
    for k,v in known.items():
        if re.search(k,t): return v
    return ""
def event_subject(title,summary,stype):
    t=(title+" "+summary).lower()
    # Use full, distinctive player names only. Short surnames (for example "James")
    # create false duplicate clusters across unrelated stories.
    names=["alejandro garnacho","levi colwill","cole palmer","joao pedro","joão pedro",
           "moises caicedo","moisés caicedo","enzo fernandez","enzo fernández",
           "reece james","estevao","estêvão","nicolo tresoldi","mike penders",
           "andrey santos","kendry paez","tyrique george","nico jackson","zubimendi","quenda"]
    for name in names:
        if re.search(r"(?<![a-z])"+re.escape(name)+r"(?![a-z])",t):
            return name.replace(" ","_")
    return ""
def date_bucket(iso):
    try: return datetime.fromisoformat(iso).date().isoformat()
    except Exception: return ""
def stable(value,n=24): return hashlib.sha256(value.encode()).hexdigest()[:n]
def fixture_key(url):
    """Extract a publisher's stable fixture ID from match report/live URLs."""
    path=urlparse(url or "").path.lower()
    m=re.search(r"/(?:report|live|commentary)/(\d+)(?:/|$)",path)
    return m.group(1) if m else ""
def observation(title,summary,url,feed,pub,published):
    title,summary,url=textval(title),textval(summary),normalize_url(url)
    if not title or not url or not relevant(title,summary) or published is None: return None
    if published>now()+timedelta(hours=12) or published<now()-timedelta(hours=MAX_AGE_HOURS): return None
    d=domain(url); pt=publisher_type(url)
    if not d or pt=="aggregator_unresolved": return None
    scope=team_scope(title,summary,url); typ=classify(title,summary)
    return {"title":title,"summary":summary[:500],"url":url,"publisher_domain":d,"publisher_name_hint":pub or None,"source_feed":feed,"published":published.isoformat(),"publisher_type":pt,"team_scope":scope,"story_type":typ,"opponent_key":opponent_key(title,summary,url),"fixture_key":fixture_key(url),"subject_key":event_subject(title,summary,typ)}
def same_event(a,b):
    # Never merge separate content categories or team scopes, even if an RSS feed
    # repeats a canonical URL with inconsistent metadata.
    if a["team_scope"]!=b["team_scope"] or a["story_type"]!=b["story_type"]: return False
    # Canonical identical article URL is safe to collapse after scope/type checks.
    if a["url"]==b["url"]: return True
    da=datetime.fromisoformat(a["published"]); db=datetime.fromisoformat(b["published"])
    if abs((da-db).total_seconds())>36*3600: return False
    typ=a["story_type"]
    if a["publisher_domain"]==b["publisher_domain"]:
        # Same-publisher articles merge only when the publisher's own stable
        # fixture ID proves they describe the identical match. Shared opponent
        # names alone are not enough: clubs can meet more than once.
        return bool(typ=="match_report" and a.get("fixture_key") and a.get("fixture_key")==b.get("fixture_key"))
    if typ in {"match_report","match_reaction","match_preview"}:
        # Across publishers, require a positively identified shared opponent.
        return bool(a["opponent_key"] and a["opponent_key"]==b["opponent_key"])
    if typ in {"transfer_rumour","confirmed_transfer"}:
        return bool(a["subject_key"] and a["subject_key"]==b["subject_key"])
    if typ=="injury_update":
        return bool(a["subject_key"] and a["subject_key"]==b["subject_key"])
    # For all other classes, only exact normalized URLs can merge.
    return False
def cluster(observations):
    groups=[]
    for o in sorted(observations,key=lambda x:x["published"]):
        target=next((g for g in groups if any(same_event(o,x) for x in g["source_observations"])),None)
        if target is None:
            identity=o["url"]
            groups.append({"event_id":stable(identity),"title":o["title"],"story_type":o["story_type"],"team_scope":o["team_scope"],"published":o["published"],"best_summary":o["summary"],"representative_title":o["title"],"representative_url":o["url"],"representative_publisher":o["publisher_domain"],"representative_publisher_type":o["publisher_type"],"source_observations":[o]})
        else:
            target["source_observations"].append(o)
            # Representative fields always come from one and the same observation.
            rank={"official_domain":4,"established_domain":3,"other_direct_domain":1}
            if rank.get(o["publisher_type"],0)>rank.get(target["representative_publisher_type"],0):
                target["representative_title"]=o["title"]; target["representative_url"]=o["url"]; target["representative_publisher"]=o["publisher_domain"]; target["representative_publisher_type"]=o["publisher_type"]
            if len(o["summary"])>len(target["best_summary"]): target["best_summary"]=o["summary"]
            target["published"]=min(target["published"],o["published"])
    for g in groups:
        g["title"]=g["representative_title"]
        g["source_observations"].sort(key=lambda x:(x["publisher_domain"],x["published"],x["url"]))
        g["source_urls"]=sorted({x["url"] for x in g["source_observations"]})
        g["publisher_domains"]=sorted({x["publisher_domain"] for x in g["source_observations"]})
        g["source_feeds"]=sorted({x["source_feed"] for x in g["source_observations"]})
        g["publisher_count"]=len(g["publisher_domains"]); g["observation_count"]=len(g["source_observations"])
        g["corroboration_status"]="multiple_publishers_observed_independence_not_proven" if g["publisher_count"]>1 else "single_publisher_observation"
    return groups
def score(g):
    p=g["representative_publisher_type"]; typ=g["story_type"]
    points=20+{"official_domain":35,"established_domain":25,"other_direct_domain":5}.get(p,0)+min(12,(g["publisher_count"]-1)*4)
    points+={"injury_update":10,"team_news":9,"confirmed_transfer":14,"match_report":10,"match_reaction":5,"match_preview":5,"transfer_rumour":-5,"opinion_analysis":-8,"general_news":0,"club_news":4}.get(typ,0)
    points+={"mens_first_team_or_club":5,"chelsea_women":2,"academy_youth":1}.get(g["team_scope"],0)
    age=max(0,(now()-datetime.fromisoformat(g["published"])).total_seconds()/3600); points-=min(12,int(age//12))
    g["importance_score"]=max(0,min(100,points))
    g["priority"]="P1_OFFICIAL_SOURCE_REVIEW" if p=="official_domain" else ("P2_ESTABLISHED_REPORT_REVIEW" if p=="established_domain" and typ!="opinion_analysis" and typ!="transfer_rumour" else ("P3_TRANSFER_RUMOUR_REQUIRES_LABEL" if typ=="transfer_rumour" else ("P4_OPINION_LOW_PRIORITY" if typ=="opinion_analysis" else "P3_SPECIALIST_OR_OTHER_SOURCE_REVIEW")))
    g.update(editorial_status="pending_editorial_verification",verification_required=True,claims_verified=False,article_generated=False,published_to_blogger=False,published_to_x=False)
def verification_checklist(g):
    """Return concrete checks for a human/automated verifier; never imply they passed."""
    common=[
        "Open the original source URL and confirm the article is accessible and dated correctly.",
        "Verify every factual claim against an official or otherwise authoritative primary source.",
        "Check whether the development is still current; record the verification time and source links.",
        "Do not treat multiple publishers as independent confirmation without checking their original sourcing."
    ]
    typ=g["story_type"]
    specific={
        "match_report":["Confirm final score, competition, match date and venue from the competition/club record.","Check that the headline describes the completed match, not a preview or reaction.","Confirm whether the story concerns the men's first team, Chelsea Women or academy."],
        "match_reaction":["Verify the match result and identify the speaker; check the exact quotation and its context.","Keep opinion/reaction separate from independently established match facts."],
        "match_preview":["Confirm fixture date, kick-off time, competition, venue and opponent from an official fixture source.","Verify availability/team news separately and label predictions as predictions."],
        "transfer_rumour":["Identify the original reporter/source and verify the exact wording; do not upgrade a rumour into fact.","Check player's current club, contract situation and any claimed fee/offer with reliable sources.","Clearly label unconfirmed interest, negotiations, bids and completed deals as different stages."],
        "confirmed_transfer":["Require an official club/league announcement before describing the move as completed.","Verify player identity, contract term and effective date from the announcement."],
        "injury_update":["Verify the injury and expected availability from a direct club/manager/medical update where available.","Do not infer a diagnosis, recovery date or match availability from vague wording."],
        "team_news":["Check the official squad/call-up/line-up announcement and the relevant competition/date.","Distinguish selection omission from injury, suspension or confirmed unavailability."],
        "opinion_analysis":["Attribute opinions and predictions to the named author/commentator; do not present them as facts.","Check any statistics or factual premises independently and add context/caveats."],
        "club_news":["Verify the exact club-policy/administrative issue and quote from the original source or an official statement.","Explain the context without implying disciplinary action or consequences that are not confirmed."],
        "general_news":["Confirm the central development and all named people, dates and figures from reliable sources."]
    }
    return common+specific.get(typ,specific["general_news"])

TERMINAL_EVENT_STATUSES = {"draft_created_unpublished", "rejected_quality_gate", "published", "archived"}

def filter_pending_candidates(groups, history):
    """Do not repeatedly generate the same event after a draft or quality rejection."""
    events = history.get("events", {}) if isinstance(history, dict) else {}
    return [g for g in groups if events.get(g.get("event_id"), {}).get("status") not in TERMINAL_EVENT_STATUSES]


def select_candidates(groups,limit=MAX_STORIES,opinion_limit=MAX_OPINION_CANDIDATES):
    selected=[]; opinions=0
    for g in groups:
        if g["story_type"]=="opinion_analysis":
            if opinions>=opinion_limit: continue
            opinions+=1
        selected.append(g)
        if len(selected)>=limit: break
    return selected

def fetch_feed(cfg,session):
    out=[]; rep={"source":cfg["name"],"url":cfg["url"],"status":"failed","entries_received":0,"observations_collected":0,"rejected_undated":0,"rejected_old":0,"rejected_irrelevant":0,"rejected_unresolved_aggregator":0,"error":None}
    try:
        r=session.get(cfg["url"],timeout=TIMEOUT); r.raise_for_status(); feed=feedparser.parse(r.content); rep["entries_received"]=len(feed.entries)
        for e in feed.entries:
            title=textval(e.get("title")); summary=textval(e.get("summary",e.get("description","")))
            if not relevant(title,summary): rep["rejected_irrelevant"]+=1; continue
            dt=parse_date(e)
            if not dt: rep["rejected_undated"]+=1; continue
            if dt>now()+timedelta(hours=12) or dt<now()-timedelta(days=MAX_AGE_HOURS): rep["rejected_old"]+=1; continue
            url=normalize_url(e.get("link"))
            if matches(domain(url),AGGREGATORS): rep["rejected_unresolved_aggregator"]+=1; continue
            src=e.get("source") or {}; pub=textval(src.get("title")) if isinstance(src,dict) else ""
            item=observation(title,summary,url,cfg["name"],pub,dt)
            if item: out.append(item)
        rep["observations_collected"]=len(out); rep["status"]="success" if out else "empty"
        if not out: rep["error"]="No eligible direct Chelsea stories in this feed"
    except Exception as ex: rep["error"]=f"{type(ex).__name__}: {ex}"; logging.warning("%s: %s",cfg["name"],rep["error"])
    return out,rep
def load_history():
    try:
        data=json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(data,dict) and isinstance(data.get("events"),dict): return data
    except Exception: pass
    return {"events":{}}
def update_history(groups,history):
    old=history.get("events",{}); ev=dict(old); stamp=now().isoformat(); cutoff=now()-timedelta(days=HISTORY_DAYS)
    for g in groups:
        k=g["event_id"]; prev=ev.get(k,{})
        ev[k]={"event_id":k,"title":g["title"][:240],"first_seen_at":prev.get("first_seen_at",stamp),"last_seen_at":stamp,"last_published_at":g["published"],"last_observation_count":g["observation_count"],"publisher_domains":g["publisher_domains"],"status":prev.get("status","observed_not_processed")}
    kept={}
    for k,v in ev.items():
        try:
            dt=datetime.fromisoformat(v["last_seen_at"].replace("Z","+00:00")); dt=dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
            if dt>=cutoff: kept[k]=v
        except Exception: continue
    return {"history_version":VERSION,"updated_at":stamp,"events":kept}
def write(path,data): path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
def run_self_tests():
    base=datetime(2026,11,10,18,tzinfo=timezone.utc)
    def o(title,url,feed,summary="",dt=base):
        return {"title":title,"summary":summary,"url":url,"publisher_domain":domain(url),"publisher_name_hint":None,"source_feed":feed,"published":dt.isoformat(),"publisher_type":publisher_type(url),"team_scope":team_scope(title,summary,url),"story_type":classify(title,summary),"opponent_key":opponent_key(title,summary,url),"fixture_key":fixture_key(url),"subject_key":event_subject(title,summary,classify(title,summary))}
    # Exact live-output regression: BBC coverage plus two different Sky URLs
    # (live commentary and match report) share the same Sky fixture ID 578259.
    h=[o("Chelsea play out goalless draw with Hacken","https://www.bbc.co.uk/sport/football/videos/c62keklwjpe0o?at_medium=RSS&at_campaign=rss", "BBC", "Chelsea maintain their unbeaten start in the Women's Champions League but were frustrated by Hacken who pick up their second point in this season's competition.", datetime(2026,11,10,20,7,12,tzinfo=timezone.utc)),o("Hacken 0-0 Chelsea commentary","https://www.skysports.com/football/bk-hcken-women-vs-chelsea-women/live/578259", "Sky", "", datetime(2026,11,10,17,15,tzinfo=timezone.utc)),o("Chelsea frustrated at Hacken in WCL","https://www.skysports.com/football/bk-hcken-women-vs-chelsea-women/report/578259", "Sky", "Chelsea remain unbeaten in the Women's Champions League but they were frustrated in a goalless draw at BK Hacken.", datetime(2026,11,10,19,0,tzinfo=timezone.utc))]
    gh=cluster(h); assert len(gh)==1 and gh[0]["team_scope"]=="chelsea_women" and gh[0]["observation_count"]==3 and gh[0]["publisher_count"]==2
    # Same publisher + same opponent is NOT enough when fixture IDs differ.
    x=[o("Chelsea draw with Hacken","https://www.skysports.com/football/bk-hcken-women-vs-chelsea-women/report/111111","Sky"),o("Chelsea beat Hacken","https://www.skysports.com/football/bk-hcken-women-vs-chelsea-women/report/222222","Sky",dt=base+timedelta(minutes=20))]
    assert len(cluster(x))==2
    # Same fixture ID cannot bridge different story stages.
    x=[o("Chelsea Women face Hacken preview","https://www.skysports.com/football/bk-hcken-women-vs-chelsea-women/report/333333","Sky"),o("Chelsea frustrated at Hacken after goalless draw","https://www.skysports.com/football/bk-hcken-women-vs-chelsea-women/live/333333","Sky")]
    assert len(cluster(x))==2
    # Unrelated BBC injury/team story and Zubimendi transfer stay apart.
    x=[o("Chelsea staff warned over Fantasy Premier League use","https://www.bbc.co.uk/sport/football/c","BBC"),o("Chelsea named as one of three teams interested in £60m January move for Zubimendi","https://www.talkchelsea.net/d","TalkChelsea")]
    assert len(cluster(x))==2
    # Everton reaction and Hacken preview are separate event types and opponents.
    x=[o("Bompastor says Chelsea were not ruthless enough after Everton draw","https://chelsea.news/e","Chelsea.news"),o("Chelsea Women face Hacken without James and Girma","https://chelsea.news/f","Chelsea.news")]
    assert len(cluster(x))==2
    # Same publisher opinion pieces must not merge.
    x=[o("The only part of Chelsea working well after Sunderland defeat","https://www.talkchelsea.net/g","TalkChelsea"),o("Why did Chelsea sign him?","https://www.talkchelsea.net/h","TalkChelsea")]
    assert len(cluster(x))==2
    assert classify("The first summer AFCON in eight years: what 2027 means for Chelsea and the Premier League","")=="opinion_analysis"
    assert classify("Steven Gerrard predicts Estevao will give Xabi Alonso a selection problem at Chelsea","")=="opinion_analysis"
    assert classify("Chelsea’s scary stats with and without injured star show bigger problem than Alonso","")=="opinion_analysis"
    assert team_scope("Manchester City break clear of Chelsea in WSL after Bronze’s freak own goal","")=="chelsea_women"
    assert team_scope("Rio Ngumoha reveals why he left Chelsea to join Liverpool","")=="academy_youth"
    assert classify("Chelsea staff warned over Fantasy Premier League use","")== "club_news"
    assert classify("Chelsea confirm Quenda has a muscle injury","")== "injury_update"
    # Player identity must use distinctive full names, never generic surname fragments.
    assert event_subject("Chelsea linked with Reece James","", "transfer_rumour")=="reece_james"
    assert event_subject("Chelsea linked with James","", "transfer_rumour")==""
    # Identical URLs cannot merge across different team scopes or story classes.
    same_url_a=o("Chelsea Women face Hacken preview","https://www.bbc.co.uk/sport/a","BBC")
    same_url_b=o("Chelsea Women beat Hacken","https://www.bbc.co.uk/sport/a","BBC")
    assert not same_event(same_url_a,same_url_b)
    # Opinion cap must not displace higher-ranked factual candidates.
    queue=[{"story_type":"opinion_analysis","importance_score":90-i} for i in range(7)]+[{"story_type":"match_report","importance_score":80},{"story_type":"team_news","importance_score":79}]
    picked=select_candidates(queue,limit=8,opinion_limit=4)
    assert sum(x["story_type"]=="opinion_analysis" for x in picked)==4 and any(x["story_type"]=="match_report" for x in picked) and any(x["story_type"]=="team_news" for x in picked)
    processed = {"events": {"done": {"status": "draft_created_unpublished"}, "held": {"status": "rejected_quality_gate"}}}
    pending = filter_pending_candidates([{"event_id":"done"},{"event_id":"held"},{"event_id":"new"}], processed)
    assert [x["event_id"] for x in pending] == ["new"]
    # Same Sunderland result, distinct publishers and clear opponent: merge.
    x=[o("Le Fée penalty sinks Chelsea at Sunderland","https://www.theguardian.com/a","Guardian"),o("Chelsea lose at Sunderland after Le Fee penalty","https://chelsea.news/i","Chelsea.news")]
    gs=cluster(x); assert len(gs)==1 and gs[0]["observation_count"]==2
    # Every representative title and URL must originate from the same observation.
    for g in gh+gs:
        assert any(s["title"]==g["representative_title"] and s["url"]==g["representative_url"] for s in g["source_observations"])
        assert set(g["source_urls"])=={s["url"] for s in g["source_observations"]}
    print("PASS: 17 regression checks (Hacken 3-way merge; fixture/stage/false-merge guards; opinion and FPL classification; injury classification; opinion queue cap; Sunderland merge; source integrity).")
def main():
    # Fail fast if a known regression is reintroduced before any live collection.
    run_self_tests()
    started=now(); old=load_history(); session=requests.Session(); session.headers.update({"User-Agent":f"Mozilla/5.0 (compatible; ChelseaFeedsBot/{VERSION}; +https://www.chelseafeeds.com/)"})
    obs=[]; reports=[]
    for cfg in FEEDS:
        logging.info("Collecting %s",cfg["name"]); items,rep=fetch_feed(cfg,session); obs.extend(items); reports.append(rep); time.sleep(.15)
    groups=cluster(obs)
    for g in groups:
        score(g)
        g["verification_status"]="not_started"
        g["verification_checklist"]=verification_checklist(g)
    pending_groups=filter_pending_candidates(groups,old)
    order={"P1_OFFICIAL_SOURCE_REVIEW":0,"P2_ESTABLISHED_REPORT_REVIEW":1,"P3_TRANSFER_RUMOUR_REQUIRES_LABEL":2,"P3_SPECIALIST_OR_OTHER_SOURCE_REVIEW":3,"P4_OPINION_LOW_PRIORITY":4}
    pending_groups.sort(key=lambda g:(order.get(g["priority"],9),-g["importance_score"],g["published"],g["title"].lower()))
    selected=select_candidates(pending_groups); stamp=now(); history=update_history(groups,old)
    statuses=Counter(r["status"] for r in reports)
    candidate={"collector_version":VERSION,"generated_at":stamp.isoformat(),"selection_limit":MAX_STORIES,"queue_semantics":"Pending candidates only. A later verification processor must explicitly approve claims before article generation or publication.","stories":selected}
    report={"collector_version":VERSION,"run_started_at":started.isoformat(),"run_completed_at":stamp.isoformat(),"duration_seconds":round((stamp-started).total_seconds(),2),"configured_sources":len(reports),"successful_sources":statuses["success"],"empty_sources":statuses["empty"],"failed_sources":statuses["failed"],"raw_observations_collected":len(obs),"unique_events_after_conservative_deduplication":len(groups),"duplicate_observations_merged":len(obs)-len(groups),"selected_pending_candidates":len(selected),"eligible_unprocessed_events":len(pending_groups),"skipped_previously_processed_events":len(groups)-len(pending_groups),"opinion_candidate_limit":MAX_OPINION_CANDIDATES,"selected_opinion_candidates":sum(g["story_type"]=="opinion_analysis" for g in selected),"events_not_selected_due_to_limit":max(0,len(pending_groups)-len(selected)),"selected_team_scope_distribution":dict(Counter(g["team_scope"] for g in selected)),"selected_story_type_distribution":dict(Counter(g["story_type"] for g in selected)),"selected_priority_distribution":dict(Counter(g["priority"] for g in selected)),"selected_events_with_multiple_publishers":sum(g["publisher_count"]>1 for g in selected),"selected_source_link_count":sum(len(g["source_urls"]) for g in selected),"source_reports":reports,"excluded_sources":[{"source":"Google News","status":"disabled","reason":"Aggregator URLs not reliably resolved to direct publisher articles."},{"source":"NewsNow","status":"disabled","reason":"Used as a discovery reference only; resolve headlines to original publisher articles and verify there."}],"quality_controls":["Conservative event clustering: ambiguous stories remain separate; Chelsea mention alone never causes a merge.","Different publishers only merge when story type, team scope, time window and event-specific opponent/player identity agree.","Different same-publisher match articles merge only when story class, team scope, time window and the publisher fixture ID all match; same opponent alone is insufficient.","Match previews, reports and reactions are separate event types.","Chelsea Women and academy scope are detected using headline, summary and URL.","Representative title, summary and URL are sourced from coherent observation records; every original URL remains attached.","RSS excerpts are capped at 500 characters and are source hints only, never article copy.","Multiple publishers do not prove independent confirmation.","All candidates remain pending editorial verification; claims_verified is always false.",
        "Opinion/analysis candidates are capped at four per run so factual reporting retains queue space.",
        "Each candidate carries a type-specific verification checklist; checklist items are instructions, not completed checks.","Collection does not generate articles or publish to Blogger or X."]}
    write(CANDIDATES_FILE,candidate); write(REPORT_FILE,report); write(HISTORY_FILE,history)
    logging.info("v%s: %d observations -> %d conservative events -> %d pending candidates",VERSION,len(obs),len(groups),len(selected))
    return 0
if __name__=="__main__":
    if "--self-test" in sys.argv: run_self_tests()
    else: raise SystemExit(main())
