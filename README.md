# ChelseaFeeds Automation

Chelsea FC news collection, evidence triage and unpublished article-draft preparation.

## Current pipeline

1. `collect_news.py` collects Chelsea-related RSS items, clusters duplicate events and leaves every candidate pending.
2. `process_news.py` checks source-page accessibility and evidence strength, then creates original Blogger-ready HTML drafts when the evidence threshold is met.
3. The `ChelseaFeeds Evidence and Article Drafts` workflow runs automatically after a successful `Chelsea News Collection` workflow on `main`.
4. Editorial results are saved as the `chelseafeeds-editorial-results` GitHub Actions artifact.

## Required repository secret

To enable AI article-draft generation, add this repository secret:

- Name: `OPENAI_API_KEY`
- Value: your OpenAI API key

GitHub path: repository **Settings → Secrets and variables → Actions → New repository secret**.

Never commit or paste the key into source files, issues, or chat. API usage may incur charges. The workflow uses `gpt-4.1-mini` by default and creates at most six drafts per run.

Without the secret, the evidence checks still run and the report records which candidates passed the evidence gate, but no article drafts are generated.

## Editorial and publishing safeguards

- Source accessibility and publisher count are evidence triage, not proof of factual accuracy.
- Two URLs from the same publisher count as one publisher.
- Factual drafts require an accessible official source or two distinct established publisher domains.
- Transfer rumours remain explicitly attributed and unconfirmed unless an official announcement supports completion.
- Opinion drafts must attribute opinions and separate them from facts.
- Every generated draft remains unpublished and requires editorial review.
- This repository does not currently publish to Blogger or X.

## Workflow artifacts

Open **Actions → ChelseaFeeds Evidence and Article Drafts → latest successful run → Artifacts**. Download `chelseafeeds-editorial-results` to inspect `editorial_report.json` and `article_drafts.json`.
