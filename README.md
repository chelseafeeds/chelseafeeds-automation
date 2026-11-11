# ChelseaFeeds Automation

Chelsea FC news collection, evidence triage, unpublished article drafting and locally generated branded thumbnails.

## Current pipeline

1. `collect_news.py` collects Chelsea-related RSS items, clusters duplicate events and leaves every candidate pending.
2. `process_news.py` checks up to eight source URLs per candidate, generates attributed article drafts with Groq (up to ten per run), applies the editorial quality gate, and creates a branded thumbnail locally with Pillow.
3. It also exports `blogger_ready_posts.json`, containing Blogger title, HTML body, search description, labels, suggested permalink slug, source URLs and featured-image metadata.
4. The `ChelseaFeeds Evidence and Article Drafts` workflow runs automatically after a successful `Chelsea News Collection` workflow on `main`.
5. Results are saved as the `chelseafeeds-editorial-results` GitHub Actions artifact, including `article_drafts.json`, `editorial_report.json` and JPG thumbnails in `images/`.

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


## Guarded Blogger publishing and optional X announcement

A separate **manual-only** workflow now exists at `.github/workflows/publish-reviewed-article.yml`. It does not run after collection and does not publish anything automatically.

### Required GitHub Actions secrets

In repository Settings → Secrets and variables → Actions, add:

**Blogger**
- `BLOGGER_CLIENT_ID` — Google OAuth client ID for a project with Blogger API v3 enabled.
- `BLOGGER_CLIENT_SECRET` — matching OAuth client secret.
- `BLOGGER_REFRESH_TOKEN` — OAuth refresh token authorized for the Blogger account that owns the blog.
- `BLOGGER_BLOG_ID` — numeric Blogger blog ID.

**X (only if you want announcements)**
- `X_API_KEY`
- `X_API_SECRET`
- `X_ACCESS_TOKEN`
- `X_ACCESS_TOKEN_SECRET`

Never put these values in repository files, workflow inputs, issues, or chat. The workflow reads them only from GitHub Actions secrets.

### How to run it safely

1. Add the Blogger secrets first. Enable Blogger API v3 in the matching Google Cloud project and authorize the OAuth client for the blog owner account.
2. Open Actions → **ChelseaFeeds Publish Reviewed Article** → Run workflow.
3. Enter the successful editorial processor run ID and the exact `event_id` from that run's `blogger_ready_posts.json`.
4. Leave **Publish publicly** unchecked for the first test. This creates a Blogger draft only.
5. Leave **Post to X** unchecked. X is only called when Blogger publication is public and the X option is explicitly selected.
6. Inspect the created Blogger draft, its formatting, labels, search description and featured image. The thumbnail must still be uploaded/set as the Blogger post image separately.
7. Only after you have reviewed the article and confirmed the Blogger draft is correct should you manually run the workflow again with **Publish publicly** checked. Select **Post to X** only if you also want the public article announced.

The publisher refuses records that do not match the expected review status and safety flags, checks required SEO fields and rejects unsafe HTML. It selects exactly one article by `event_id`. X announcements are blocked unless Blogger returns a public post URL from a live publication.

**Important:** this is a controlled publishing tool, not unattended auto-publishing. The editorial workflow continues to produce review-required drafts. Credentials must be added by the repository owner; this integration cannot read or create repository secrets on your behalf.

## Workflow artifacts

Open Actions → ChelseaFeeds Evidence and Article Drafts → latest successful run → Artifacts. Download `chelseafeeds-editorial-results`. It includes `blogger_ready_posts.json` for copying into Blogger HTML view and the `images/` folder for featured thumbnails. Upload each thumbnail separately in Blogger; review the draft and sources before publishing. Upload the thumbnail to Blogger and set it as the post's featured image before publishing.
