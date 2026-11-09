# Threads publishing

Threads uses the same GitHub queue pattern as Instagram. Reviewed rows live in
`threads_queue.csv`, the Action checks every three hours, and durable state lives
in `threads_state.json`.

## Queue columns

| Column | Purpose |
|---|---|
| `post_id` | Permanent unique ID. Never reuse one. |
| `release_at` | UTC time in `YYYY-MM-DD HH:MM:SS` format. |
| `status` | Use `queued` to make a reviewed row eligible. |
| `post_type` | `question`, `styling`, `gifting`, `brand`, or `product`. |
| `text` | Threads copy, maximum 500 characters. |
| `media_type` | `TEXT` or `IMAGE`. |
| `image_url` | Public HTTPS image URL for image posts. |
| `alt_text` | Plain description of the image. |
| `product_url` | Product page retained for planning and reporting. |
| `utm_campaign` | Tracking campaign used in any link added to `text`. |

The included rows use `status=example`, so they can never publish accidentally.

## Content mix

Threads should not repeat Pinterest descriptions. A useful five-post week is:

- Two conversational questions
- One styling thought
- One gifting or brand observation
- One product-led image post with a tracked link

Use approved clean product images more often than Pinterest text overlays.
Publishing is automated, but replies should remain human-reviewed at first.

## Required GitHub secrets

Add these under **Settings > Secrets and variables > Actions**:

- `THREADS_TOKEN`: long-lived Threads API access token
- `THREADS_USER_ID`: numeric Threads user ID for the Amiees profile

Optional repository variables:

- `THREADS_EXPECTED_USERNAME`: exact connected username, used as a safety check
- `THREADS_API_VERSION`: defaults to `v1.0`
- `THREADS_MIN_GAP_HOURS`: defaults to `16`

The token needs the Threads permissions required for basic profile access and
content publishing. Add insights or reply-management permissions only when
those features are enabled.

## First test

1. Add the two required secrets.
2. Add one reviewed row with `status=queued` and a future UTC time.
3. Open **Actions > Post to Threads > Run workflow**.
4. Leave `dry_run` enabled and verify the selected row.
5. Run it again with `dry_run` disabled.

The scheduled workflow publishes no more than one post per run. A post is
checkpointed before the public publish request, and ambiguous results stop the
queue for manual reconciliation rather than risking a duplicate.
