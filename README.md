# ChelseaFeeds Automation

Chelsea FC news collection, evidence triage, unpublished article drafting and mixed licensed-photo / AI-illustration featured images.

## Current pipeline

1. `collect_news.py` collects Chelsea-related RSS items, clusters duplicate events and leaves every candidate pending.
2. `process_news.py` checks up to eight source URLs per candidate, generates attributed article drafts with Groq (up to ten per run), applies the editorial quality gate, and creates a temporary branded fallback thumbnail.\n3. `generate_featured_images.py` replaces that fallback where possible with a relevant, licence-screened Wikimedia Commons photo; otherwise it requests an article-aware AI illustration if `OPENAI_API_KEY` is configured.
3. It also exports `blogger_ready_posts.json`, containing Blogger title, HTML body, search description, labels, suggested permalink slug, source URLs and featured-image metadata.
4. The `ChelseaFeeds Evidence and Article Drafts` workflow runs automatically after a successful `Chelsea News Collection` workflow on `main`.
5. Results are saved as the `chelseafeeds-editorial-results` GitHub Actions artifact, including `article_drafts.json`, `editorial_report.json` and JPG thumbnails in `images/`.

## Article generation (Groq)

The processor uses Groq's OpenAI-compatible API.

1. Create a Groq API key in the official console: https://console.groq.com/keys
2. In GitHub repository Settings → Secrets and variables → Actions, create `GROQ_API_KEY`.
3. Save the key only as a GitHub Actions secret. Never commit it or paste it into issues/chat.

Default model: `openai/gpt-oss-120b`. Model availability and rate limits can change.

## Mixed featured-image generation and sourcing

The featured-image pipeline now follows this order for every article:

1. **Relevant licensed photograph:** search Wikimedia Commons using the article headline and context. A candidate must pass relevance, image-size and licence checks. Only CC0, public-domain, or CC BY 2.0/3.0/4.0 files are accepted automatically. The original file page, creator, licence and attribution are saved with the article.
2. **AI-generated editorial illustration:** if no sufficiently relevant licensed photo is found, use the OpenAI Images API to create a scene based on the article headline, excerpt and visual prompt. It must be an illustration, not fake documentary photography or an imitation of a real player's likeness. The generated image carries an "AI ILLUSTRATION" disclosure.
3. **Branded fallback:** if neither source is available, retain the previous locally generated ChelseaFeeds graphic. The report records which method was used.

All final images are normalized to 1200 × 675 JPEGs. Commons images receive a visible creator/source/licence credit in the Blogger article. AI images are identified as illustrations. News-publisher and social-media images are never scraped.

### Enable AI-generated fallback

In GitHub repository Settings → Secrets and variables → Actions, optionally add:

- `OPENAI_API_KEY` — an OpenAI API key with image-generation API access.

The image workflow uses `gpt-image-2.5-flare` by default. Image generation is a paid API operation; check current API pricing and organization/model access before adding the key. Without this secret, the system still attempts a properly licensed Commons photograph and otherwise uses the branded fallback. The existing Groq key does not automatically provide OpenAI image-generation access.

The system never treats an AI illustration as a real match photograph. A Commons licence does not automatically clear every possible personality, trademark or other non-copyright restriction; source files still need appropriate editorial review.

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

Never put these values in repository files, workflow inputs, issues, or chat. The workflow reads them only from GitHub Actions secrets.

### How to run it safely

1. Add the four Blogger secrets listed above. Enable Blogger API v3 in the matching Google Cloud project and authorize the OAuth client for the blog owner account.
2. For article-aware AI image fallback, optionally add `OPENAI_API_KEY` as a GitHub Actions secret. Without it, the workflow still searches Commons and uses the branded fallback when no suitable photo is found.
3. Open Actions → **ChelseaFeeds Publish Reviewed Article** → Run workflow.
4. Enter the successful editorial processor run ID and the exact `event_id` from that run's `blogger_ready_posts.json`.
5. The workflow automatically uploads the selected featured image, validates its source metadata and creates or updates the matching Blogger draft. There are no public-publish or X-posting switches.
6. Inspect the Blogger draft, including image relevance, visible image credit or AI disclosure, formatting, labels and search description.

The publisher refuses records that do not match the expected review status and safety flags, checks required SEO fields and rejects unsafe HTML. It selects exactly one article by `event_id`, refuses ambiguous duplicate drafts and refuses to run in live-publishing mode.

**Important:** this is a controlled draft-testing tool, not unattended auto-publishing. Public publishing remains hard-disabled in the current publisher code.

## Workflow artifacts

Open Actions → ChelseaFeeds Evidence and Article Drafts → latest successful run → Artifacts. Download `chelseafeeds-editorial-results` to inspect `blogger_ready_posts.json`, `article_drafts.json`, the editorial report and generated images. The manual draft workflow uploads the selected image to the public ChelseaFeeds image repository and embeds it, together with its credit or AI disclosure, in the Blogger draft.


## Draft-only featured-image publishing test

The manual Blogger workflow now accepts only the source workflow run ID and exact article event ID. It has no public-publish or X-posting switches. The publisher itself also rejects a live-publishing request, and always creates or updates a Blogger draft. Check the Blogger draft and the image credit/disclosure before any future change to publication permissions.
