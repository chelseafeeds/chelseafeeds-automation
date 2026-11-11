# ChelseaFeeds Automation

Chelsea FC news collection, evidence triage, unpublished article drafting and branded editorial image assets.

## Current pipeline

1. `collect_news.py` collects Chelsea-related RSS items, clusters duplicate events and leaves every candidate pending.
2. `process_news.py` v1.2 checks source accessibility and evidence strength, generates a draft with Groq, applies an automated editorial quality gate, and optionally generates two branded image assets.
3. The `ChelseaFeeds Evidence and Article Drafts` workflow runs automatically after a successful `Chelsea News Collection` workflow on `main`.
4. Results are saved as the `chelseafeeds-editorial-results` GitHub Actions artifact, including `article_drafts.json`, `editorial_report.json` and generated JPG files in `images/`.

## Article generation (Groq)

The processor uses Groq's OpenAI-compatible API.

1. Create a Groq API key in the official console: https://console.groq.com/keys
2. In GitHub repository Settings → Secrets and variables → Actions, create `GROQ_API_KEY`.
3. Save the key only as a GitHub Actions secret. Never commit it or paste it into issues/chat.

Default model: `openai/gpt-oss-120b`. The free plan has changing model availability and rate limits. Groq and Cloudflare are separate services and credentials.

## Free-first image generation (Cloudflare Workers AI)

The image service is Cloudflare Workers AI using `@cf/black-forest-labs/flux-1-schnell`. Official documentation:
- Model/API: https://developers.cloudflare.com/workers-ai/models/flux-1-schnell/
- REST API: https://developers.cloudflare.com/workers-ai/
- Pricing and free daily allocation: https://developers.cloudflare.com/workers-ai/platform/pricing/

Cloudflare currently documents a free allocation of 10,000 Neurons per day on Workers Free. The allocation resets daily at 00:00 UTC; when a Free account exceeds it, further inference requests fail rather than being charged. Paid-plan usage above the free allocation is billed, so do not upgrade or enable paid billing without reviewing and approving the account's billing settings. Provider pricing and quotas can change.

### Configure the two required GitHub secrets

1. Sign in to Cloudflare and open the Workers AI dashboard for your account.
2. Copy your Cloudflare Account ID from the account dashboard.
3. Create a Cloudflare API token with Workers AI Write permission (minimum required permission for model inference). Restrict it to the relevant account where possible.
4. In this GitHub repository, open Settings → Secrets and variables → Actions → New repository secret.
5. Add:
   - Name: `CLOUDFLARE_ACCOUNT_ID` — Value: your Cloudflare Account ID.
   - Name: `CLOUDFLARE_API_TOKEN` — Value: the API token.
6. Never put either value in source files, workflow YAML, commits, screenshots or chat. Do not send the token to anyone.

If either secret is missing, article drafting and the editorial report still run; image generation is skipped gracefully. Image failures do not delete or block an otherwise quality-passed article draft.

### Image and branding rules

- Up to three articles per run; up to six image requests total (one featured thumbnail and one supporting inline image per article).
- Four inference steps per image; no automatic retry, to avoid repeated quota use.
- Images are normalized to 1200 × 675 JPEG.
- Pillow adds the exact `CHELSEAFEEDS` wordmark at the bottom-left on every successful generated image, using a navy panel, white text and gold outline. The AI is not asked to render the brand lettering.
- Image prompts request editorial illustrations, not fake match photography or exact depictions of real players.
- The thumbnail and supporting image are separate assets. The supporting image is inserted into article HTML using an `IMAGE_UPLOAD_REQUIRED/` placeholder.
- Generated images are in the artifact's `images/` folder. Before Blogger publication, upload each image to Blogger and replace the placeholder with its hosted image URL. The processor does not upload images or publish posts.
- Image ALT text and descriptive filenames are included in the draft metadata.

## Processor v1.2 quality and safety checks

- Source accessibility and publisher counts are evidence triage, not factual verification.
- Factual stories require an accessible official source or two distinct established publisher domains; opinion requires an accessible source and attribution.
- Automated quality checks cover substantive article length, title and description fields, headings, source links, allowed source URLs and unsafe HTML. A passing score is not a fact-check.
- Direct quotations are not requested; unsupported claims should be omitted or flagged.
- Transfer reports must remain attributed and unconfirmed unless official evidence confirms completion.
- Generated images are illustrations, not documentary evidence.
- Every article remains `draft_requires_editorial_review`; `claims_verified` remains false.
- Blogger and X publishing are disabled. No Blogger or X credentials are read.

## Workflow artifacts

Open Actions → ChelseaFeeds Evidence and Article Drafts → latest successful run → Artifacts. Download `chelseafeeds-editorial-results` and inspect the JSON reports and image files.
