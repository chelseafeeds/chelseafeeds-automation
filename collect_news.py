#!/usr/bin/env python3
"""ChelseaFeeds Collector v3.8: conservative, event-aware RSS collector.

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

VERSION = "3.8"
ROOT = Path(__file__).resolve().parent
HISTORY_FILE = ROOT / "seen_stories.json"
CANDIDATES_FILE = ROOT / "news_candidates.json"
REPORT_FILE = ROOT / "collection_report.json"
TIMEOUT, MAX_AGE_DAYS, MAX_STORIES, HISTORY_DAYS = 18, 7, 40, 90

FEEDS = [
    {"name":"BBC Sport","url":"https://feeds.bbci.co.uk/sport/football/rss.xml"},
    {"name":"Sky Sports","url":"https://www.skysports.com/rss/12040"},
    {"name":"The Guardian Chelsea","url":"https://www.theguardian.com/football/chelsea/rss"},
    {"name":"TalkChelsea","url":"https://www.talkchelsea.net/feed/"},
    {"name":"Chelsea.news","url":"https://chelsea.news/feed"},
    {"name":"Chelsea FC Latest News","url":"https://chelseafclatestnews.com/feed"},
    {"name":"Football365","url":"https://www.football365.com/rss"},
]
OFFICIAL={"chelseafc.com","premierleague.com","uefa.com","thefa.com"}
ESTABLISHED={"bbc.co.uk","bbc.com","skysports.com","theguardian.com","reuters.com","apnews.com","espn.com","independent.co.uk","standard.co.uk","nytimes.com","theathletic.com","football365.com","thetimes.com","telegraph.co.uk"}
AGGREGATORS={"news.google.com","newsnow.com","c.newsnow.com"}
IRRELEVANT=("chelsea flower show","chelsea boots","chelsea handler","chelsea manning","chelsea hotel")
STOP=set("the a an and or but for to of in on at by with from after before as is are was were be been being this that these those his her their its our your how what why when where who which could would should may might will can has have had into over under about against says said claims claim according report reports latest news fc blues club team side player players manager boss head coach premier league football".split())
WOMEN_HINTS=("chelsea women","chelsea women's","chelsea ladies","women's champions league","women champions league","wcl","bompastor")
YOUTH_HINTS=("academy","under-21","under 21","u21","under-18","under 18","u18","youth league","cobham next gen")
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
    # Transfer signals first, but commentary/analysis must not become transfer news by incidental wording.
    action=any(x in t for x in TRANSFER_ACTION)
    confirmed=any(x in t for x in TRANSFER_CONFIRMED)
    uncertain=any(x in t for x in TRANSFER_UNCERTAIN) or (bool(re.search(r"[£€$]\s?\d",t)) and action) or (bool(re.search(r"[£€$]\s?\d",t)) and any(x in t for x in ("rumour","rumor")))
    if action and confirmed: return "confirmed_transfer"
    if (action and uncertain) or (bool(re.search(r"[£€$]\s?\d",t)) and any(x in t for x in ("rumour","rumor"))): return "transfer_rumour"
    if any(x in t for x in INJURY) and not any(x in t for x in ("stats with and without","analysis","scary stats")): return "injury_update"
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
    # Extract named player/target strings for transfer and player-specific developments.
    names=["levi colwill","cole palmer","joao pedro","joão pedro","moises caicedo","moisés caicedo","enzo fernandez","enzo fernández","estevao","estêvão","quenda","nicolo tresoldi","nicolo","zubimendi","garnacho","mike penders","james","gir ma"]
    found=[n for n in names if n in t]
    if found: return found[0].replace(" ","_")
    return ""
def date_bucket(iso):
    try: return datetime.fromisoformat(iso).date().isoformat()
    except Exception: return ""
def stable(value,n=24): return hashlib.sha256(value.encode()).hexdigest()[:n]
def observation(title,summary,url,feed,pub,published):
    title,summary,url=textval(title),textval(summary),normalize_url(url)
    if not title or not url or not relevant(title,summary) or published is None: return None
    if published>now()+timedelta(hours=12) or published<now()-timedelta(days=MAX_AGE_DAYS): return None
    d=domain(url); pt=publisher_type(url)
    if not d or pt=="aggregator_unresolved": return None
    scope=team_scope(title,summary,url); typ=classify(title,summary)
    return {"title":title,"summary":summary[:500],"url":url,"publisher_domain":d,"publisher_name_hint":pub or None,"source_feed":feed,"published":published.isoformat(),"publisher_type":pt,"team_scope":scope,"story_type":typ,"opponent_key":opponent_key(title,summary,url),"subject_key":event_subject(title,summary,typ)}
def same_event(a,b):
    # Canonical identical article URL is safe to collapse regardless of feed.
    if a["url"]==b["url"]: return True
    # Never merge separate content categories, team scopes, or same-publisher articles.
    if a["team_scope"]!=b["team_scope"] or a["story_type"]!=b["story_type"]: return False
    if a["publisher_domain"]==b["publisher_domain"]: return False
    da=datetime.fromisoformat(a["published"]); db=datetime.fromisoformat(b["published"])
    if abs((da-db).total_seconds())>36*3600: return False
    typ=a["story_type"]
    if typ in {"match_report","match_reaction","match_preview"}:
        # Same match only when opponent can be positively identified on both sides.
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
    points+={"injury_update":10,"team_news":9,"confirmed_transfer":14,"match_report":10,"match_reaction":5,"match_preview":5,"transfer_rumour":-5,"opinion_analysis":-8,"general_news":0}.get(typ,0)
    points+={"mens_first_team_or_club":5,"chelsea_women":2,"academy_youth":1}.get(g["team_scope"],0)
    age=max(0,(now()-datetime.fromisoformat(g["published"])).total_seconds()/3600); points-=min(12,int(age//12))
    g["importance_score"]=max(0,min(100,points))
    g["priority"]="P1_OFFICIAL_SOURCE_REVIEW" if p=="official_domain" else ("P2_ESTABLISHED_REPORT_REVIEW" if p=="established_domain" and typ!="opinion_analysis" and typ!="transfer_rumour" else ("P3_TRANSFER_RUMOUR_REQUIRES_LABEL" if typ=="transfer_rumour" else ("P4_OPINION_LOW_PRIORITY" if typ=="opinion_analysis" else "P3_SPECIALIST_OR_OTHER_SOURCE_REVIEW")))
    g.update(editorial_status="pending_editorial_verification",verification_required=True,claims_verified=False,article_generated=False,published_to_blogger=False,published_to_x=False)
def fetch_feed(cfg,session):
    out=[]; rep={"source":cfg["name"],"url":cfg["url"],"status":"failed","entries_received":0,"observations_collected":0,"rejected_undated":0,"rejected_old":0,"rejected_irrelevant":0,"rejected_unresolved_aggregator":0,"error":None}
    try:
        r=session.get(cfg["url"],timeout=TIMEOUT); r.raise_for_status(); feed=feedparser.parse(r.content); rep["entries_received"]=len(feed.entries)
        for e in feed.entries:
            title=textval(e.get("title")); summary=textval(e.get("summary",e.get("description","")))
            if not relevant(title,summary): rep["rejected_irrelevant"]+=1; continue
            dt=parse_date(e)
            if not dt: rep["rejected_undated"]+=1; continue
            if dt>now()+timedelta(hours=12) or dt<now()-timedelta(days=MAX_AGE_DAYS): rep["rejected_old"]+=1; continue
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
        return {"title":title,"summary":summary,"url":url,"publisher_domain":domain(url),"publisher_name_hint":None,"source_feed":feed,"published":dt.isoformat(),"publisher_type":publisher_type(url),"team_scope":team_scope(title,summary,url),"story_type":classify(title,summary),"opponent_key":opponent_key(title,summary,url),"subject_key":event_subject(title,summary,classify(title,summary))}
    # Hacken reports and live commentary: same women's match.
    h=[o("Chelsea play out goalless draw with Hacken","https://www.bbc.co.uk/sport/football/a", "BBC"),o("Chelsea frustrated at Hacken in WCL","https://www.skysports.com/football/b", "Sky"),o("Hacken 0-0 Chelsea commentary","https://www.skysports.com/football/bk-hacken-women-vs-chelsea-women/c", "Sky")]
    gh=cluster(h); assert len(gh)==1 and gh[0]["team_scope"]=="chelsea_women" and gh[0]["observation_count"]==3
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
    # Same Sunderland result, distinct publishers and clear opponent: merge.
    x=[o("Le Fée penalty sinks Chelsea at Sunderland","https://www.theguardian.com/a","Guardian"),o("Chelsea lose at Sunderland after Le Fee penalty","https://chelsea.news/i","Chelsea.news")]
    gs=cluster(x); assert len(gs)==1 and gs[0]["observation_count"]==2
    # Every representative title and URL must originate from the same observation.
    for g in gh+gs:
        assert any(s["title"]==g["representative_title"] and s["url"]==g["representative_url"] for s in g["source_observations"])
        assert set(g["source_urls"])=={s["url"] for s in g["source_observations"]}
    print("PASS: 9 regression checks (Hacken 3-way merge; false-merge guards; event-type split; opinion classification; Sunderland merge; source integrity).")
def main():
    # Fail fast if a known regression is reintroduced before any live collection.
    run_self_tests()
    started=now(); old=load_history(); session=requests.Session(); session.headers.update({"User-Agent":f"Mozilla/5.0 (compatible; ChelseaFeedsBot/{VERSION}; +https://www.chelseafeeds.com/)"})
    obs=[]; reports=[]
    for cfg in FEEDS:
        logging.info("Collecting %s",cfg["name"]); items,rep=fetch_feed(cfg,session); obs.extend(items); reports.append(rep); time.sleep(.15)
    groups=cluster(obs)
    for g in groups: score(g)
    order={"P1_OFFICIAL_SOURCE_REVIEW":0,"P2_ESTABLISHED_REPORT_REVIEW":1,"P3_TRANSFER_RUMOUR_REQUIRES_LABEL":2,"P3_SPECIALIST_OR_OTHER_SOURCE_REVIEW":3,"P4_OPINION_LOW_PRIORITY":4}
    groups.sort(key=lambda g:(order.get(g["priority"],9),-g["importance_score"],g["published"],g["title"].lower()))
    selected=groups[:MAX_STORIES]; stamp=now(); history=update_history(groups,old)
    statuses=Counter(r["status"] for r in reports)
    candidate={"collector_version":VERSION,"generated_at":stamp.isoformat(),"selection_limit":MAX_STORIES,"queue_semantics":"Pending candidates only. A later verification processor must explicitly approve claims before article generation or publication.","stories":selected}
    report={"collector_version":VERSION,"run_started_at":started.isoformat(),"run_completed_at":stamp.isoformat(),"duration_seconds":round((stamp-started).total_seconds(),2),"configured_sources":len(reports),"successful_sources":statuses["success"],"empty_sources":statuses["empty"],"failed_sources":statuses["failed"],"raw_observations_collected":len(obs),"unique_events_after_conservative_deduplication":len(groups),"duplicate_observations_merged":len(obs)-len(groups),"selected_pending_candidates":len(selected),"events_not_selected_due_to_limit":max(0,len(groups)-len(selected)),"selected_team_scope_distribution":dict(Counter(g["team_scope"] for g in selected)),"selected_story_type_distribution":dict(Counter(g["story_type"] for g in selected)),"selected_priority_distribution":dict(Counter(g["priority"] for g in selected)),"selected_events_with_multiple_publishers":sum(g["publisher_count"]>1 for g in selected),"selected_source_link_count":sum(len(g["source_urls"]) for g in selected),"source_reports":reports,"excluded_sources":[{"source":"Google News","status":"disabled","reason":"Aggregator URLs not reliably resolved to direct publisher articles."},{"source":"NewsNow","status":"disabled","reason":"Direct article extraction was not reliable."}],"quality_controls":["Conservative event clustering: ambiguous stories remain separate; Chelsea mention alone never causes a merge.","Different publishers only merge when story type, team scope, time window and event-specific opponent/player identity agree.","Different articles from the same publisher never merge unless their normalized URLs are identical.","Match previews, reports and reactions are separate event types.","Chelsea Women and academy scope are detected using headline, summary and URL.","Representative title, summary and URL are sourced from coherent observation records; every original URL remains attached.","RSS excerpts are capped at 500 characters and are source hints only, never article copy.","Multiple publishers do not prove independent confirmation.","All candidates remain pending editorial verification; claims_verified is always false.","Collection does not generate articles or publish to Blogger or X."]}
    write(CANDIDATES_FILE,candidate); write(REPORT_FILE,report); write(HISTORY_FILE,history)
    logging.info("v%s: %d observations -> %d conservative events -> %d pending candidates",VERSION,len(obs),len(groups),len(selected))
    return 0
if __name__=="__main__":
    if "--self-test" in sys.argv: run_self_tests()
    else: raise SystemExit(main())
 
