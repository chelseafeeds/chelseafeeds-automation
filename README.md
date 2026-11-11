# ChelseaFeeds Automation

Chelsea FC news collection, evidence triage, unpublished article drafting and locally generated branded thumbnails.

## Current pipeline

1. `collect_news.py` collects Chelsea-related RSS items, clusters duplicate events and leaves every candidate pending.
2. `process_news.py` checks source accessibility and evidence strength, generates article drafts with Groq, applies the editorial quality gate, and creates a branded thumbnail locally with Pillow.
3. The `ChelseaFeeds Evidence and Article Drafts` workflow runs automatically after a successful `Chelsea News Collection` workflow on `main`.
4. Results are saved as the `chelseafeeds-editorial-results` GitHub Actions artifact, including `article_drafts.json`, `editorial_report.json` and JPG thumbnails in `images/`.

## Article generation (Groq)

The processor uses Groq's OpenAI-compatible API.

1. Create a Groq API key in the official console: https://console.groq.com/keys
2. In GitHub repository Settings → Secrets and variables → Actions, create `GROQ_API_KEY`.
3. Save the key only as a GitHub Actions secret. Never commit it or paste it into issues/chat.

Default model: `openai/gpt-oss-120b`. Model availability and rate limits can change.

## Thumbnail generation — no external image service

ChelseaFeeds thumbnails are now generated entirely inside GitHub Actions using Pillow. **Cloudflare Workers AI and its secrets are no longer required.**

Each thumbnail:
- Is exported as a 1200 × 675 JPEG, suitable for a Blogger featured image.
- Uses Chelsea blue, deep navy, white and gold.
- Includes the article headline and CHELSEAFEEDS wordmark.
- Uses abstract football/stadium graphics, not fabricated club crests, real-player likenesses or fake match photography.
- Is saved in the workflow artifact under `images/`, with a descriptive filename and ALT text in the article draft JSON.

The design is deterministic: the same article title and layout rules produce a consistent branded style. It does not call an image-generation API, use API credentials, incur image inference charges, or download unverified photographs.

### Optional real football photographs

A later enhancement can use appropriately licensed photographs from Wikimedia Commons or another rights-cleared source. This is deliberately not part of the automatic pipeline yet: each image's licence, attribution and relevance must be checked before reuse. Do not automatically scrape images from news publishers or social media.

## Editorial quality and publishing safeguards

- Source accessibility and publisher counts are evidence triage, not factual verification.
- Factual stories require an accessible official source or two distinct established publisher domains; opinion requires an accessible attributed source.
- Automated quality checks cover substantive article length, title and description fields, headings, source links, allowed source URLs and unsafe HTML.
- Direct quotations are not requested; unsupported claims should be omitted or flagged.
- Every article remains `draft_requires_editorial_review`; `claims_verified` remains false.
- Blogger and X publishing are disabled. No Blogger or X credentials are read.
- A quality-passed article is not discarded if thumbnail creation fails; the image status is reported separately.

## Workflow artifacts

Open Actions → ChelseaFeeds Evidence and Article Drafts → latest successful run → Artifacts. Download `chelseafeeds-editorial-results`. The `images/` folder contains the ready-to-upload thumbnails. Upload the thumbnail to Blogger and set it as the post's featured image before publishing.
