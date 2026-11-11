import os
import smtplib
import json
from email.message import EmailMessage
from pathlib import Path

event_path = os.environ.get("GITHUB_EVENT_PATH")
if not event_path or not Path(event_path).is_file():
    raise SystemExit("GitHub workflow_run event payload is missing.")

event = json.loads(Path(event_path).read_text(encoding="utf-8"))
failed = event.get("workflow_run", {})
workflow_name = failed.get("name", "ChelseaFeeds workflow")
conclusion = failed.get("conclusion", "unknown")
run_url = failed.get("html_url", "")
run_id = failed.get("id", "")
branch = failed.get("head_branch", "unknown")
commit = (failed.get("head_sha") or "")[:12]
started = failed.get("run_started_at", "unknown")
finished = failed.get("updated_at", "unknown")

if conclusion != "failure":
    print(f"No alert sent: workflow conclusion is {conclusion}.")
    raise SystemExit(0)

recipient = os.environ["MONITOR_EMAIL"]
username = os.environ["SMTP_USERNAME"]
password = os.environ["SMTP_APP_PASSWORD"]

message = EmailMessage()
message["Subject"] = f"[ChelseaFeeds ALERT] {workflow_name} failed"
message["From"] = username
message["To"] = recipient
message.set_content(
    "ChelseaFeeds automation failure detected.\n\n"
    f"Workflow: {workflow_name}\n"
    f"Conclusion: {conclusion}\n"
    f"Branch: {branch}\n"
    f"Run ID: {run_id}\n"
    f"Commit: {commit}\n"
    f"Started: {started}\n"
    f"Last updated: {finished}\n\n"
    f"Open the failed GitHub Actions run: {run_url}\n\n"
    "This alert is automatic. Check the failed job and logs, fix the issue, "
    "then allow the next scheduled run or rerun the workflow."
)

with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
    server.ehlo()
    server.starttls()
    server.ehlo()
    server.login(username, password)
    server.send_message(message)

print(f"Failure alert sent to {recipient} for workflow run {run_id}.")
