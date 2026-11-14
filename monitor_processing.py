#!/usr/bin/env python3
"""Scheduled monitor for ChelseaFeeds article-processing runs and their artifacts."""
from __future__ import annotations
import json, os, re, sys, time, zipfile, io
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse
import requests
from PIL import Image

OWNER, REPO = "chelseafeeds", "chelseafeeds-automation"
API = f"https://api.github.com/repos/{OWNER}/{REPO}"
TOKEN = os.environ["GITHUB_TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28"}
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ChelseaFeeds-Run-Monitor/1.0"})
REPORT = {"checked_at": datetime.now(timezone.utc).isoformat(), "runs": [], "summary": {}}

class Anchors(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if href: self.links.append(href)

def api_get(url, **kwargs):
    r=requests.get(url, headers=HEADERS, timeout=30, **kwargs)
    r.raise_for_status()
    return r

def is_live_article(url):
    p=urlparse(url or "")
    return (p.scheme=="https" and p.netloc.lower() in {"www.chelseafeeds.com","chelseafeeds.com"}
            and re.fullmatch(r"/\d{4}/\d{2}/[^/]+\.html",p.path or "") is not None)

def find_json(zf, suffix):
    matches=[n for n in zf.namelist() if n.endswith(suffix)]
    if not matches: return None
    return json.loads(zf.read(matches[0]).decode("utf-8"))

def inspect_artifact(run, artifact):
    result={"run_number":run.get("run_number"),"run_id":run["id"],"status":"completed",
            "artifact_name":artifact.get("name"),"articles":[],"errors":[]}
    if artifact.get("expired"):
        result["errors"].append("Editorial artifact has expired.")
        return result
    blob=api_get(f"{API}/actions/artifacts/{artifact['id']}/zip")
    zf=zipfile.ZipFile(io.BytesIO(blob.content))
    drafts=find_json(zf,"article_drafts.json")
    ready=find_json(zf,"blogger_ready_posts.json")
    editorial=find_json(zf,"editorial_report.json")
    if drafts is None or ready is None or editorial is None:
        result["errors"].append("One or more required JSON files are missing from the artifact.")
        return result
    draft_items=drafts.get("drafts",[]) if isinstance(drafts,dict) else []
    posts=ready.get("posts",[]) if isinstance(ready,dict) else []
    by_event={str(x.get("event_id")):x for x in draft_items}
    image_names={n.rsplit("/",1)[-1]:n for n in zf.namelist() if "/images/" in n or n.startswith("images/")}
    for post in posts:
        event=str(post.get("event_id",""))
        draft_item=by_event.get(event,{})
        draft=draft_item.get("draft",{})
        html=post.get("content_html") or draft.get("article_html") or ""
        parser=Anchors(); parser.feed(html)
        internal=[u for u in parser.links if is_live_article(u)]
        declared=[x.get("url") for x in draft.get("related_internal_links",[]) if isinstance(x,dict)]
        related_section=bool(re.search(r"Related ChelseaFeeds coverage",html,re.I))
        bad_internal=[u for u in internal if not is_live_article(u)]
        # Verify actual published destination rather than merely trusting the hostname.
        link_checks=[]
        for url in sorted(set(internal))[:8]:
            try:
                rr=SESSION.get(url,timeout=12,allow_redirects=True,headers={"Range":"bytes=0-0"})
                link_checks.append({"url":url,"http_status":rr.status_code,"ok":rr.status_code<400})
            except Exception as exc:
                link_checks.append({"url":url,"ok":False,"error":type(exc).__name__})
        image=post.get("featured_image") or draft.get("featured_image") or {}
        filename=image.get("filename") or (image.get("local_path","").split("/")[-1])
        image_entry=image_names.get(filename)
        image_check={"present":False,"valid":False,"filename":filename,"source_type":image.get("source_type"),
                     "alt_text":image.get("alt_text") or post.get("image_alt_text"),
                     "attribution":image.get("credit_text") or image.get("creator"),
                     "license":image.get("license_name")}
        if image_entry:
            raw=zf.read(image_entry)
            image_check["present"]=True
            try:
                im=Image.open(io.BytesIO(raw)); im.verify()
                im=Image.open(io.BytesIO(raw))
                image_check.update(valid=im.format=="JPEG",width=im.width,height=im.height,bytes=len(raw))
                image_check["valid"]=bool(im.format=="JPEG" and im.size==(1200,675) and len(raw)>1000)
            except Exception as exc:
                image_check["error"]=type(exc).__name__
        quality=draft.get("editorial_quality") or draft_item.get("quality_gate") or {}
        article={"event_id":event,"title":post.get("title") or draft.get("title"),
                 "editorial_status":post.get("editorial_status"),
                 "claims_verified":post.get("claims_verified"),
                 "quality_gate_passed":quality.get("passed"),
                 "word_count":quality.get("word_count"),
                 "source_urls":post.get("source_urls") or draft_item.get("source_urls",[]),
                 "internal_link_count":len(internal),"related_section_present":related_section,
                 "declared_internal_link_count":len(declared),"internal_link_checks":link_checks,
                 "image":image_check}
        issues=[]
        if not article["title"]: issues.append("Missing title")
        if not html: issues.append("Missing article HTML")
        if quality.get("passed") is False: issues.append("Editorial quality gate failed")
        if declared and not related_section: issues.append("Related-link metadata exists but section is missing")
        if any(not x["ok"] for x in link_checks): issues.append("One or more internal links failed HTTP verification")
        if not image_check["present"] or not image_check["valid"]: issues.append("Featured image missing or invalid (expected 1200x675 JPEG)")
        if not image_check["alt_text"]: issues.append("Featured image ALT text missing")
        if image_check["source_type"]=="licensed_commons_photo" and not image_check["attribution"]: issues.append("Commons photo attribution missing")
        article["issues"]=issues
        result["articles"].append(article)
    processing_results=editorial.get("results",[]) if isinstance(editorial.get("results"),list) else []
    status_counts={}
    for item in processing_results:
        status=item.get("processing_status","unknown")
        status_counts[status]=status_counts.get(status,0)+1
        error=str(item.get("error") or "")
        if "AI provider retries exhausted" in error or "HTTP 429" in error:
            result["errors"].append(f"AI article generation was rate-limited for candidate: {item.get('headline') or item.get('event_id')}")
    result["editorial_report"]={"input_candidates":editorial.get("input_candidate_count"),
        "drafts_created":editorial.get("drafts_created"),"blogger_ready_export_count":editorial.get("blogger_ready_export_count"),
        "processing_status_counts":status_counts,
        "article_publishing_enabled":editorial.get("article_publishing_enabled"),
        "x_publishing_enabled":editorial.get("x_publishing_enabled"),
        "featured_image_pipeline":editorial.get("featured_image_pipeline")}
    if (editorial.get("input_candidate_count") or 0)>0 and (editorial.get("drafts_created") or 0)==0:
        result["errors"].append("No articles were generated from this processing run.")
    if len(posts)!=(editorial.get("drafts_created") or 0):
        result["errors"].append("Blogger-ready post count does not match the editorial draft count.")
    result["errors"].extend([f"{a.get('title')}: {issue}" for a in result["articles"] for issue in a["issues"]])
    result["verification"]="passed" if not result["errors"] else "needs_attention"
    return result

def watchdog_collection():
    """Recover a missed 5-minute cron without starting overlapping collection runs."""
    url=f"{API}/actions/workflows/collect-news.yml/runs?branch=main&per_page=10"
    runs=api_get(url).json().get("workflow_runs",[])
    active=[r for r in runs if r.get("status") in {"queued","in_progress","waiting","requested"}]
    latest=max(runs,key=lambda r:r.get("created_at",""),default=None)
    age_minutes=None
    if latest and latest.get("created_at"):
        age_minutes=(datetime.now(timezone.utc)-datetime.fromisoformat(latest["created_at"].replace("Z","+00:00"))).total_seconds()/60
    result={"latest_collection_run_id":latest.get("id") if latest else None,
            "latest_collection_created_at":latest.get("created_at") if latest else None,
            "latest_collection_age_minutes":round(age_minutes,1) if age_minutes is not None else None,
            "active_collection_runs":len(active),"dispatch_attempted":False,"dispatch_result":"not_needed"}
    # Scheduled GitHub Actions events can be delayed or dropped. Dispatch only
    # when no run is active and the last collection is older than eight minutes.
    if not active and (age_minutes is None or age_minutes > 8):
        result["dispatch_attempted"]=True
        response=requests.post(f"{API}/actions/workflows/collect-news.yml/dispatches",
            headers=HEADERS,json={"ref":"main"},timeout=30)
        if response.status_code==204:
            result["dispatch_result"]="dispatched"
        else:
            result["dispatch_result"]=f"HTTP {response.status_code}: {response.text[:300]}"
            response.raise_for_status()
    return result

def main():
    REPORT["collection_watchdog"]=watchdog_collection()
    runs=api_get(f"{API}/actions/workflows/process-news.yml/runs?branch=main&per_page=15").json().get("workflow_runs",[])
    active=[r for r in runs if r.get("status") in {"queued","in_progress","waiting","requested"}]
    REPORT["active_runs"]=[{"run_number":r.get("run_number"),"id":r["id"],"status":r.get("status"),
        "created_at":r.get("created_at"),"url":r.get("html_url")} for r in active]
    for run in runs:
        if run.get("status")!="completed" or run.get("conclusion")!="success": continue
        artifacts=api_get(f"{API}/actions/runs/{run['id']}/artifacts?per_page=20").json().get("artifacts",[])
        artifact=next((a for a in artifacts if a.get("name")=="chelseafeeds-editorial-results"),None)
        if not artifact: continue
        try: REPORT["runs"].append(inspect_artifact(run,artifact))
        except Exception as exc:
            REPORT["runs"].append({"run_number":run.get("run_number"),"run_id":run["id"],
                "verification":"monitor_error","errors":[type(exc).__name__+": "+str(exc)[:300]]})
        if len(REPORT["runs"])>=5: break
    REPORT["summary"]={"active_processing_runs":len(active),
        "completed_runs_inspected":len(REPORT["runs"]),
        "articles_inspected":sum(len(r.get("articles",[])) for r in REPORT["runs"]),
        "runs_needing_attention":sum(r.get("verification")!="passed" for r in REPORT["runs"])}
    os.makedirs("monitor-output",exist_ok=True)
    with open("monitor-output/monitor_report.json","w",encoding="utf-8") as f:
        json.dump(REPORT,f,ensure_ascii=False,indent=2)
    lines=["# ChelseaFeeds processing monitor",f"Checked: {REPORT['checked_at']}","",
        f"- Active processing runs: **{len(active)}**",
        f"- Completed runs inspected: **{len(REPORT['runs'])}**",
        f"- Articles inspected: **{REPORT['summary']['articles_inspected']}**",
        f"- Runs needing attention: **{REPORT['summary']['runs_needing_attention']}**",""]
    for r in REPORT["runs"]:
        lines += [f"## Run #{r.get('run_number')} — {r.get('verification','unknown')}",
                  f"Run: https://github.com/{OWNER}/{REPO}/actions/runs/{r.get('run_id')}"]
        for a in r.get("articles",[]):
            im=a.get("image",{})
            lines.append(f"- **{a.get('title')}** — {len(a.get('issues',[]))} issue(s); internal links {a.get('internal_link_count')}; image {im.get('source_type')} {im.get('width')}×{im.get('height')}")
            for issue in a.get("issues",[]): lines.append(f"  - WARNING: {issue}")
        for error in r.get("errors",[]): lines.append(f"- WARNING: {error}")
        lines.append("")
    lines += ["## Active runs"]
    for r in active: lines.append(f"- Run #{r.get('run_number')}: {r.get('status')} — {r.get('html_url')}")
    with open(os.environ.get("GITHUB_STEP_SUMMARY","monitor-output/summary.md"),"a",encoding="utf-8") as f:
        f.write("\n".join(lines)+"\n")
    print(json.dumps(REPORT["summary"],ensure_ascii=False))
    # Verification warnings should be visible but must not prevent the monitor from running again.
if __name__=="__main__": main()
