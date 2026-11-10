# TikTok notification publishing

TikTok publishing uses GitHub for the reviewed queue and Buffer for a mobile
notification. The final sound selection and publish tap happen in TikTok:

```text
tiktok_queue.csv -> tiktok_publish.py -> Buffer notification -> TikTok app
```

This is intentional. TikTok does not expose its music library to Buffer's
automatic publishing API. A notification preserves the ability to choose a
currently trending Commercial Sound at posting time.

## What is automated

* The reviewed 9:16 video, caption, hashtags and product link
* Daily eligibility and spacing checks
* Exact Buffer account and TikTok channel validation
* Duplicate protection with a durable checkpoint before the Buffer mutation
* The recommended sound mood in the GitHub log and state file
* A phone notification when the post is ready

## What Gabriel does on the phone

1. Install the Buffer mobile app and allow push notifications.
2. Tap the Buffer notification.
3. Continue to TikTok and choose **Add sound**.
4. Search TikTok's **Commercial Sounds** for a currently trending track matching
   the `sound_search` recommendation in `tiktok_state.json` or the Actions log.
5. Preview the post, confirm the AI disclosure if TikTok asks, and tap Publish.

For Amiees promotional posts, use Commercial Sounds rather than the general
music library unless Amiees separately holds the necessary commercial license.

## Queue fields

| Field | Purpose |
|---|---|
| `post_id` | Stable unique identifier. Never reuse it. |
| `release_at` | Earliest notification time in UTC. |
| `status` | `queued` schedules a reminder. Any other value holds it. |
| `product` | Product handle for internal tracking. |
| `caption` | Public TikTok caption, maximum 2,200 characters and five hashtags. |
| `video_url` | Public MP4, MOV or WEBM URL that remains available through posting. |
| `video_sha256` | Reviewed hosted-video checksum for audit and change detection. |
| `is_ai_generated` | TikTok AI disclosure metadata, `true` or `false`. |
| `sound_search` | Internal sound guidance. It is never included in the public caption. |
| `product_url` | Product destination for tracking and editorial review. |

The workflow checks every three hours and schedules at most one notification per
run, with at least 20 hours between newly scheduled reminders. Push-triggered and
manual runs default to dry run. Scheduled runs create notifications.

## Manual validation

```bash
python3 -m unittest -v test_tiktok_publish
DRY_RUN=1 python3 tiktok_publish.py
```

The existing `BUFFER_API_KEY` GitHub secret is reused. The publisher is pinned to
Buffer channel `673d4900da2e3cb64c96536a`, TikTok account `@amiees.com`.
