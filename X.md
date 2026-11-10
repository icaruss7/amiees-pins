# X publishing

The X publisher uses the same GitHub queue and durable-state pattern as the
Instagram, Pinterest and Threads publishers. Reviewed posts live in
`x_queue.csv`; publication state lives in `x_state.json`.

GitHub sends each due post to Buffer's API. Buffer then auto-publishes it to X,
so the workflow does not call X's paid API directly and does not consume X API
credits.

## Cadence and content mix

The X queue follows the same cadence as Threads:

- Conversational, styling and gifting posts
- Product-image posts with tracked links
- No automatic replies, likes, follows or unsolicited messages

The workflow checks every three hours, publishes at most one due post, and keeps
at least 16 hours between posts. Replies and engagement remain human-reviewed.

## Required Buffer setup

1. Connect the Amiees X account in Buffer. Buffer calls the service `twitter`
   in its API even though the public product name is X.
2. Use the existing `BUFFER_API_KEY` repository secret. The key needs
   `accountRead`, `postsRead` and `postsWrite` permissions.
3. Set `X_EXPECTED_USERNAME` to the exact X handle without `@`.
4. Run **Post to X** manually with `dry_run` enabled.
5. Confirm the log identifies the expected Buffer X channel.

Buffer's Free plan supports up to three connected channels, ten queued posts per
channel at a time, and API access. This workflow submits only a single due post
per run, so it does not fill Buffer with the entire CSV at once.

The old direct X API credentials can remain stored as a rollback option, but the
active workflow does not use them.

## Safe launch procedure

1. Connect `@AmieesShop` to Buffer.
2. Verify the Buffer API key includes `postsWrite`.
3. Run **Post to X** manually with `dry_run` enabled.
4. Confirm that the log says `Connected Buffer X channel: @AmieesShop`.
5. Let the scheduled workflow publish the first due row, or explicitly run a
   reviewed live test.

Normal pushes are always dry runs. Scheduled runs are live after the Buffer
channel and API key are configured.

## Duplicate safeguards

- Every row has a permanent `post_id` and content fingerprint.
- State is committed before the Buffer publication request.
- Ambiguous publishing results stop the queue for reconciliation.
- Published rows are skipped even when they remain `queued` in the CSV.
- The Buffer channel must match `X_EXPECTED_USERNAME`, be connected, and be
  unlocked by the current plan.
- Images use reviewed public HTTPS URLs, and alt text is passed to Buffer.

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
