# Instagram Reels through the existing GitHub publisher

The existing creative tools, Shopify Files uploads and GitHub scheduling remain
the workflow. `ig_publish.py` supports images, full carousels and approved Reels.
No Reel is added automatically. Pinterest's RSS feed remains image-only.

## First real Reel

1. Finish the Reel with the current Claude/Codex/local model process. Keep the
   approved source MP4 unchanged. Settle its caption, cover and audio separately.
   The publisher sends the MP4's embedded audio; it does not add music.
2. Create an Instagram REELS package following
   [CONTENT-PIPELINE.md](CONTENT-PIPELINE.md). Include the exact source reference,
   final MP4, 9:16 JPEG cover, caption and manifest, then write `READY` last.
   Use `hook_placement: overlay` when the hook is already in the video.
3. Run ingestion and creative approval. Generate the upload request, then have
   Claude/Codex use the existing trusted Shopify Files tools. Wait for READY.
   Record actual Shopify IDs and CDN URLs with `record-upload`.
4. Download the MP4 from that exact registered public CDN URL into the package
   as `_pipeline/hosted-video.mp4`. Watch it, including the audio. Shopify may
   have processed the master, so do not copy the master's checksum blindly.
5. Run `review-hosted-video` below. It validates the downloaded rendition and
   records its checksum and named reviewer against this upload receipt.
6. Prepare a queue draft for an actually free slot at least an hour ahead.
   Review the caption, cover/video URLs, hosted checksum and UTC time. Separately
   approve scheduling, then explicitly apply, inspect the diff, and commit/push
   the registry and queue together. These are live scheduling actions.

Example commands after creative approval and a real upload receipt:

```powershell
python content_pipeline.py review-hosted-video --package content/finished/exp-dolphin-reel-v4--instagram --file _pipeline/hosted-video.mp4 --reviewed-by Gabe
python content_pipeline.py prepare-queue --package content/finished/exp-dolphin-reel-v4--instagram --at "CHOSEN_ISO_TIME_WITH_OFFSET"
# Read the draft before running either of the following live scheduling steps.
python content_pipeline.py approve-schedule --package content/finished/exp-dolphin-reel-v4--instagram --reviewed-by Gabe
python content_pipeline.py apply-queue --package content/finished/exp-dolphin-reel-v4--instagram --confirm-add-to-live-queue
git diff -- ig_queue.csv content/experiments.json
git add ig_queue.csv content/experiments.json
git commit -m "Schedule approved Amiees Reel experiment"
git push
```

The Nature Series occupies daily slots through December 17, 2026. The existing
20-hour minimum gap leaves no room for an extra Reel between daily image posts.
Choose a genuinely free slot after the series or obtain a separate schedule
decision from Gabe. This upgrade does not move or replace any existing post.

## Queue and experiment linkage

`ig_queue.csv` retains its six existing columns and original bytes. A pipeline
Reel appends a normal row with `post_id=ig-exp-<experiment_id>`, an empty `images`
cell and `status=queued`. The publisher resolves that ID in
`content/experiments.json` and checks the exact row against both approvals.
The queue draft additionally contains:

```json
{
  "media": {
    "media_type": "REELS",
    "video_url": "ACTUAL_SHOPIFY_VIDEO_CDN_URL",
    "video_sha256": "ACTUAL_SHA256_OF_REVIEWED_HOSTED_RENDITION",
    "cover_url": "ACTUAL_SHOPIFY_COVER_CDN_URL"
  }
}
```

These are fields inside the approved draft, not new CSV columns. The original
master hashes, READY upload receipt, hosted rendition review, creative approval
and schedule approval remain in the registry. A missing record, changed row or
changed approval binding stops publishing. Publication state keeps the same
queue ID, so the existing analytics collector and lineage sync can join the
published media ID to its experiment. Missing metrics stay unknown.

For older external tooling, the publisher also understands explicit optional
`media_type,video_url,video_sha256,cover_url` CSV columns. The ingestion CLI does
not introduce those columns or rewrite an existing queue header. Use its sidecar
registry workflow for Amiees packages.

## Configuration and publishing behavior

The existing `IG_TOKEN` secret is reused. No new paid API or token is required.
The publishing workflow keeps cron `37 */3 * * *`, one post per run and the
20-hour minimum gap between normal posts. Explicitly approved additional Reels
use the spacing described below. Its manual run defaults to dry run. Scheduled runs continue
normal publishing; deployment alone adds no new queue items.

The workflow sets `IG_EXPECTED_USERNAME=amiees.com_`, `IG_DURABLE_GIT=1` and
`IG_VIDEO_PREFIX` to the repository variable when supplied, otherwise
`https://cdn.shopify.com/videos/c/o/v/`. The prefix pins an HTTPS origin/path.
That Shopify video path is global, not proof that the file belongs to Amiees;
the reviewed upload receipt establishes that association. The publisher rejects
redirects and requires the hosted MP4 to match the approved checksum, with a
100 MB local limit. Replacing CDN files after review is unsupported.

The pipeline currently requires 9:16 H.264 MP4, 3 to 180 seconds, at least
720 pixels wide, AAC if audio is present, and a valid cover. `ffprobe` must be
available locally or supplied by `--ffprobe` / `AMIEES_FFPROBE`.

The publisher creates a REELS container with the approved MP4, cover, caption
and `share_to_feed=true`; it waits until processing finishes, then publishes.
This uses Meta's documented [Reels publishing flow](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api?entity=request-23987686-3fe78620-2258-44b6-893f-42d76c7200d7).
Local validation is conservative but does not replace Meta's final processing.

## Recovery without duplicate posts

State is atomically saved and pushed to GitHub after preparation, before the
public publish request, after its returned media ID, and after permalink lookup.
If GitHub cannot save a checkpoint, the next public request stops. A conflicting
remote change must be reconciled with a normal pull/rebase, never a forced push.

A lost response after `media_publish` leaves `phase=publishing`. Such an entry,
or `needs_review`, pauses new account publishing until reconciled. Do not erase
state, reset attempts or give the same creative a new ID to bypass this pause.
Check the Instagram profile and the container status first. If it is public,
record the actual matching media ID, permalink and observed publication time in
the existing entry and set `phase=published`. If it is conclusively unpublished,
resolve the cause and deliberately restore a reusable FINISHED container to
`ready`. ERROR/EXPIRED containers need deliberate replacement after confirming
that no public post exists. Commit/push reconciled state before resuming.

An unsuccessful permalink lookup retains the successful media ID and marks
`link_pending`; it never triggers a second publication. Existing legacy
media IDs, timestamps or permalinks also prevent republishing.

## Verification limits

Run `python -m unittest -v test_ig_publish test_content_pipeline test_ig_content_planner`.
Tests exercise image/carousel compatibility, hosted checksums, approval binding,
Reels creation, lineage, lost responses and isolated Git checkpoint races.
`DRY_RUN=1 python ig_publish.py` reads local metadata only, even with a token.
It makes no Meta calls, downloads or state writes.

An actual Shopify upload and first public Reel remain a separate approved test.
Passing unit tests does not establish live video acceptance or publication.

## Native Instagram music and extra Reel slots

The read-only `Instagram Audio Catalog` workflow uses the existing secret to
retrieve the current trending catalog through Meta's
[Audio API](https://developers.facebook.com/documentation/instagram-platform/content-publishing/audio-api).
It never publishes. Its artifact retains track IDs and metadata without temporary
download links. A trending catalog entry is a selection signal, not evidence of
future reach or proof of a commercial music license.

An Instagram REELS manifest may include:

```json
{
  "is_ai_generated": true,
  "schedule_mode": "additional_reel",
  "instagram_audio": {
    "configuration": {"audio_id": "123", "audio_volume": 70, "video_volume": 1},
    "title": "Verified track title",
    "artist": "Verified artist",
    "catalog_collected_at": "2026-10-30T00:00:00Z",
    "rights_review": {
      "commercial_use_verified": true,
      "by": "Actual reviewer",
      "at": "2026-10-30T00:00:00Z",
      "source_url": "https://www.facebook.com/sound/collection/"
    }
  }
}
```

This example ID is a placeholder. Review the actual track and its permitted use
before approval. Do not set the commercial review flag from an absent API field.
Record the exact matching Sound Collection entry or other documented permission.
The publisher sends `audio_configuration` on container creation, attaching native
Instagram audio instead of baking a downloaded song into the MP4. Meta does not
support API audio previews, so verify the sound on the first published Reel.
Changing the track or volumes invalidates the prepared container fingerprint and
the separately approved media draft. AI disclosure uses Meta's
[documented `is_ai_generated` parameter](https://developers.facebook.com/documentation/instagram-platform/content-publishing).

`additional_reel` is opt-in for approved Instagram Reels only. It allows eight
hours between that Reel and neighboring publications; ordinary Nature Series
posts remain subject to twenty hours between each other. Neither the ingestion
step nor runtime guard moves existing queue rows. The normal scheduler still
runs every three hours and may delay a post to preserve spacing. A routine
overdue row may coexist with an extra slot at least twelve hours in the future;
an uncertain publication or recorded error still blocks that addition.

Instagram music clearance applies only to the permitted Meta destinations.
Pinterest videos require separately cleared music and the native video publishing
route. They must never be inserted into the image-only RSS queue.
