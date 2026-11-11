# ChelseaFeeds Automation

Chelsea FC news collection, evidence triage and unpublished article-draft preparation.

## Current pipeline

1. `collect_news.py` collects Chelsea-related RSS items, clusters duplicate events and leaves every candidate pending.
2. `process_news.py` checks source-page accessibility and evidence strength, then creates original Blogger-ready HTML drafts when the evidence threshold is met.
3. The `ChelseaFeeds Evidence and Article Drafts` workflow runs automatically after a successful `Chelsea News Collection` workflow on `main`.
4. Editorial results are saved as the `chelseafeeds-editorial-results` GitHub Actions artifact.

## Free AI setup (Groq)

The processor now uses Groq's OpenAI-compatible API by default. Groq offers a free developer plan with per-model rate limits; this is not unlimited service and quotas/model availability can change.

1. Create a Groq account and API key in the official Groq console: https://console.groq.com/keys
2. In this GitHub repository, open **Settings → Secrets and variables → Actions → New repository secret**.
3. Set the secret name to `GROQ_API_KEY` and paste the key value into GitHub's secret-value field. Never paste it into source code, issues, or ChatGPT.
4. Save the secret, then run **Actions → Chelsea News Collection → Run workflow**. The downstream editorial workflow will run after collection succeeds.

Default configuration:
- Provider: Groq
- Model: `openai/gpt-oss-120b`
- API endpoint: `https://api.groq.com/openai/v1`
- Maximum: three draft-generation attempts per run
- Source context: up to four sources, with excerpts capped at 3,500 characters each

The processor can also be configured with `AI_API_KEY`, `AI_BASE_URL`, `AI_MODEL`, and `AI_PROVIDER` environment variables. These are already set in the workflow except for the secret value.

**Important:** Free access is subject to rate limits and provider terms. If Groq changes its free plan or the selected model is unavailable, generation may fail; collection and evidence reporting should still run. No payment method or upgrade is required for the Groq free plan.

## Editorial and publishing safeguards

- Source accessibility and publisher count are evidence triage, not proof of factual accuracy.
- Two URLs from the same publisher count as one publisher.
- Factual drafts require an accessible official source or two distinct established publisher domains.
- Transfer rumours remain explicitly attributed and unconfirmed unless an official announcement supports completion.
- Opinion drafts must attribute opinions and separate them from facts.
- Every generated draft remains unpublished and requires editorial review.
- No claims are marked verified automatically.
- This repository does not publish to Blogger or X. Draft generation does not authorize publication.

## Workflow artifacts

Open **Actions → ChelseaFeeds Evidence and Article Drafts → latest successful run → Artifacts**. Download `chelseafeeds-editorial-results` to inspect `editorial_report.json` and `article_drafts.json`.
