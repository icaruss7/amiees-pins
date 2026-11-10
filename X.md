# X publishing

The X publisher uses the same GitHub queue and durable-state pattern as the
Instagram, Pinterest and Threads publishers. Reviewed posts live in
`x_queue.csv`; publication state lives in `x_state.json`.

## Cadence and content mix

The initial pilot contains four posts per week for four weeks:

- Two conversational, styling or gifting posts
- Two product-image posts with tracked links
- No automatic replies, likes, follows or unsolicited messages

The workflow checks every four hours, publishes at most one due post, and keeps
at least 20 hours between posts. Replies and engagement remain human-reviewed.

## Required X setup

Create an app at `https://console.x.com` under the Amiees X account. Give the
app **Read and write** user permissions. Generate the OAuth 1.0a credentials for
the account that owns the app.

Add these repository secrets under **Settings > Secrets and variables > Actions**:

- `X_API_KEY`
- `X_API_SECRET`
- `X_ACCESS_TOKEN`
- `X_ACCESS_TOKEN_SECRET`

Optional repository variables:

- `X_EXPECTED_USERNAME`: exact Amiees X handle, without `@`
- `X_MIN_GAP_HOURS`: defaults to `20`

The X API uses consumption-based billing. The repository never stores or prints
the secret values.

## Safe launch procedure

1. Add all four secrets.
2. Run **Post to X** manually with `dry_run` enabled.
3. Confirm that the log says `Connected X account: @...` and that the correct
   queued row is selected.
4. Run it again with `dry_run` disabled to publish the first due row.

Normal pushes are always dry runs. Scheduled runs are live after credentials
are configured. Missing credentials leave publishing disabled without failing
the workflow.

## Duplicate safeguards

- Every row has a permanent `post_id` and content fingerprint.
- State is committed before the public post request.
- Ambiguous publishing results stop the queue for reconciliation.
- Published rows are skipped even when they remain `queued` in the CSV.
- Images are downloaded only from validated public HTTPS hosts and are capped at
  X's 5 MB single-image limit.

## Queue columns

| Column | Purpose |
|---|---|
| `post_id` | Permanent unique identifier |
| `release_at` | UTC time in `YYYY-MM-DD HH:MM:SS` format |
| `status` | `queued` makes a reviewed row eligible |
| `post_type` | `question`, `styling`, `gifting`, `brand`, or `product` |
| `text` | X copy, at most 280 weighted characters |
| `media_type` | `TEXT` or `IMAGE` |
| `image_url` | Public HTTPS image URL for image posts |
| `alt_text` | Image description, at most 1,000 characters |
| `product_url` | Product page retained for planning |
| `utm_campaign` | Tracking campaign used in the post link |
