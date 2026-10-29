# Finished content pipeline

This wraps the existing creative process:

```text
planner recommendation or your own brief
  -> Claude/Codex/local model creates the finished content as before
  -> package folder -> local validation -> creative approval
  -> existing Shopify Files upload -> READY receipt with CDN URLs
  -> queue draft -> separate schedule approval -> explicit local append
  -> commit/push -> existing publisher -> publication evidence -> analytics
```

There is no new generation engine, upload service, automatic content approval or
automatic queue application. The only daily background job added by this layer
reconciles metadata for items that have already been scheduled.

## Existing conventions preserved

- Instagram: `ig_queue.csv` has `post_id,release_at,product,caption,images,status`.
  `images` is an ordered, pipe-separated list. The live publisher supports JPEG
  images, 2 to 10 image carousels, and separately approved Reels resolved from
  `content/experiments.json`. Reels have an empty `images` cell; video/cover URLs
  and the reviewed hosted checksum live in the approved registry draft. It checks due dates every three hours and
  normally spaces publications at least 20 hours apart. `ig_state.json` holds
  returned media IDs, timestamps and permalinks.
- Pinterest: `pins.csv` has `guid,board,title,description,link,image,keywords,release_at,status`.
  The three board names are Bee & Animal Jewelry, Botanical Jewelry and Celestial
  Jewelry. The existing RSS generator emits JPEG image enclosures, not video.
- Both queues store UTC as `YYYY-MM-DD HH:MM:SS`. New operator-entered times must
  include an offset; the pipeline converts them to UTC. Existing rows are untouched.
- Current images use Amiees Shopify Files under
  `cdn.shopify.com/s/files/1/0677/3201/3371/files/`, often with size/crop parameters.
  Nature assets commonly use `nature-<product>.jpg`; two-image posts use a pipe.
- Existing creative folders, including `output/nature-series/reels/<product>/`,
  remain the sources. Nothing generates into or rewrites those folders.

## New folder structure

```text
content_pipeline.py
content_assets.py
ig_pipeline_media.py          # verifies approved registry media for the publisher
requirements-content-pipeline.txt
content/
  manifest.schema.json       # finished-package intent schema
  experiments.json           # tracked metadata and lifecycle, initially empty
  finished/
    README.md
    <experiment_id>--<platform>/   # ignored by Git
      manifest.json
      caption.txt
      source/
        reference.jpg         # original reference or approved source; PNG is fine here
      final/
        01.jpg                # IMAGE; 01.jpg, 02.jpg, ... for CAROUSEL
        video.mp4             # REELS/VIDEO instead of image pages
        cover.jpg             # required with video.mp4
      READY                   # create LAST, once files are fully written
      upload-receipt.json     # supplied by your existing uploader
      _pipeline/
        upload-request.json   # generated, not an upload
        queue-draft.json      # generated, not a live queue entry
        hosted-video.mp4      # downloaded Shopify rendition, watched before review
```

An existing finished folder can be used instead with `init --folder <path>`.
The tool refuses to replace an existing manifest. Within a package, paths are
relative, use forward slashes and cannot escape through traversal or symlinks.
Media filenames can differ if declared in the manifest. Keep final carousel
pages in their intended order in `final_assets`; filesystem order is not used.

Known product handles come from the existing Instagram queue. For a new product,
first verify its real Shopify handle/page through your existing Shopify access,
then register that attestation without inserting a dummy publishing row:

```powershell
python content_pipeline.py register-product --product VERIFIED_HANDLE --url https://amiees.com/products/VERIFIED_HANDLE --verified-by Gabe
```

The registry records this under `verified_products`. This is human-attested identity,
not an automatic stock check; current availability still needs review.

Large media and local receipts are ignored only under `content/finished/`.
If you choose another folder, keep it outside Git or add an appropriate ignore
rule yourself. Never commit API keys, customer information or local credentials.

## Manifest and registry

`content/manifest.schema.json` describes a **finished** package. `init` deliberately
creates an incomplete scaffold that stays `needs_content` until filled. The CLI
validates fields, media and cross-field rules before approval. Example intent:

```json
{
  "schema_version": 1,
  "experiment_id": "exp-dolphin-worn-v1",
  "product": "dolphin-dreams-ring",
  "platform": "instagram",
  "format": "REELS",
  "content_angle": "nature_detail",
  "hook": "Spot the dolphin.",
  "hook_placement": "overlay",
  "hypothesis": "The dolphin reveal may encourage profile visits; this needs a test.",
  "objective": "Product consideration",
  "primary_success_metric": {
    "name": "profile_visits_per_reached_account",
    "definition": "Profile visits divided by reached accounts, with raw counts retained"
  },
  "planner_reference": null,
  "source_assets": ["source/reference.png"],
  "final_assets": [
    {"path": "final/video.mp4", "role": "video", "alt": "Dolphin ring worn beside an ocean scene."},
    {"path": "final/cover.jpg", "role": "cover", "alt": "Dolphin ring close-up."}
  ],
  "caption_file": "caption.txt",
  "scheduled_at": null,
  "pinterest": {
    "board": "", "title": "", "description": "", "destination_url": "", "keywords": []
  }
}
```

`experiment_id` is stable and unique for a test revision. Use a new ID after a
scheduled creative needs changing. One experiment can have separate Instagram
and Pinterest packages; they must refer to the same product. Formats are
Instagram IMAGE/CAROUSEL/REELS and Pinterest IMAGE/VIDEO. Instagram Reels can
proceed after hosted-rendition review. Pinterest VIDEO stays blocked because its
current RSS publisher only supports images.

`hook_placement=caption_opening` checks the caption's first line against the hook.
For an existing video with its own overlay, use `overlay`; a human checks that
overlay. Do not change a reviewed video simply to satisfy a scaffold's example.

When seeded from the planner, `planner_reference` snapshots its recommendation
ID, file hash, review-week start, test type and historical control. The creative
fields and metric are copied into the manifest. A later weekly plan can change
without erasing the original experiment brief.

Seeding Pinterest from an Instagram recommendation maps image/carousel ideas to
one IMAGE (Reels to VIDEO) and flags `platform_adaptation_required`. Review the
Pinterest metric, hypothesis and creative rather than treating that adaptation
as the same controlled Instagram comparison.

The tracked **runtime manifest** is the item in `content/experiments.json`, keyed
by `<experiment_id>--<platform>`. It contains the intent plus:

| Field | Meaning |
|---|---|
| `publishing_status` | Current observed lifecycle stage |
| `content_sha256` | Exact manifest, caption, ordered assets and reference hashes |
| `source_assets`, `final_assets` | Validated relative paths, byte hashes and media properties |
| `approval` | Creative reviewer, time and approved fingerprint |
| `shopify_files`, `shopify_cdn_urls` | Attested READY file IDs, local hashes and actual CDN URLs |
| `queue_draft`, `schedule_approval` | Exact row/time, base queue hash and separate review |
| `scheduled_at` | Actual chosen UTC slot once applied; `requested_scheduled_at` retains optional intent |
| `queue_id` | Existing publisher's stable post_id or Pin guid |
| `published_post_id`, `permalink` | Returned Instagram identity or attested Pinterest result |
| `analytics_linkage` | Analytics file/key, collection time, experiment ID and actual counts |
| `history`, `revisions` | State events and retained superseded creative revisions |

Do not hand-edit runtime status to skip a step. Source manifest edits are safe:
the next scan invalidates old approvals, upload receipts and queue drafts. Already
scheduled items refuse in-place revision and need a new experiment ID.

## Lifecycle

```text
needs_content -> ready_for_review -> approved -> uploaded
              -> scheduled -> published -> measured
```

- `needs_content`: files, metadata, READY marker or validation are incomplete.
- `ready_for_review`: decoded files and required metadata pass local checks.
- `approved`: a named reviewer approved this exact fingerprint.
- `uploaded`: the existing uploader returned verified READY Shopify file IDs/URLs.
- `scheduled`: the exact separately approved row exists in the local queue. It
  reaches GitHub only after you commit and push the registry and queue together.
- `published`: Instagram state records publication, or a person supplies a
  verified Pinterest public Pin receipt. RSS release alone never qualifies.
- `measured`: matching Instagram post insights are available, or verified
  Pinterest metrics are supplied. This means data exists, not that the test won
  or that the public creative passed a visual inspection.

## How to create and ingest the next real package

Run from a fresh checkout of `icaruss7/amiees-pins`:

```powershell
git pull --ff-only
python -m pip install -r requirements-content-pipeline.txt
python content_pipeline.py init --from-plan test-2026-10-29-01 --platform instagram
```

Use a recommendation ID from the **current** `ig_content_plan.json`; the example
above is the first October 29 plan item. For a manual brief or an already-finished
video, use an explicit ID instead:

```powershell
python content_pipeline.py init --experiment-id exp-dolphin-reel-v4 --product dolphin-dreams-ring --platform instagram --format REELS
```

Then have Claude/Codex do the following using your existing tools:

1. Fill `manifest.json`. For a controlled planner follow-up, preserve the named
   control's media/order and other text; change only the agreed variable.
2. Copy the relevant original reference into `source/` and the existing final
   exports into `final/`. Update declared paths if filenames differ. Leave the
   original creative folders untouched.
3. Supply the exact reviewed caption in `caption.txt`. For video, include the
   final approved cover. Final images/covers are JPEG. Pinterest RSS images use
   2:3 at least 1000 pixels wide; Instagram uses this pipeline's conservative
   4:5 through 1.91:1 JPEG presets. No automatic cropping or re-encoding happens.
4. Video validation uses an existing `ffprobe`, on PATH or set through
   `AMIEES_FFPROBE`. It checks portrait H.264 MP4, 3 to 180 seconds, width >=720,
   and AAC audio when present. If ffprobe is unavailable, video approval stops;
   a filename alone is not proof of a valid video.
5. Write a file named `READY` last, then trigger ingestion:

```powershell
python content_pipeline.py scan
# Or watch the finished folder until Ctrl+C; never approves or queues anything:
python content_pipeline.py watch --interval 30
# Existing external finished folder:
python content_pipeline.py ingest --package C:/path/to/finished-package
```

Review the report and actual creative, then approve and prepare the upload handoff:

```powershell
python content_pipeline.py approve --package content/finished/exp-dolphin-reel-v4--instagram --reviewed-by Gabe
python content_pipeline.py upload-request --package content/finished/exp-dolphin-reel-v4--instagram
```

`--reviewed-by` records the actual reviewer. It is an audit record, not an account
login or a substitute for Gabe's authorization. This CLI is local, not a public
service. The exclusive registry lock prevents overlapping local mutations; after
a crash inspect the PID in `.pipeline.lock` before manually removing a stale lock.

## Shopify boundary: keep the existing upload workflow

This repo has no implemented Shopify upload client/workflow. The local script
cannot borrow Claude/ChatGPT connector credentials. It therefore stops at
`_pipeline/upload-request.json`, with approved paths, alt text, hashes and unique
suggested filenames. It makes no upload or remote HTTP request.

Claude/Codex uses the existing authorized Shopify Files tools, or you upload in
Shopify Admin under Content > Files. Reuse an existing identical approved File
when appropriate. Upload the exact requested exports, wait for `fileStatus=READY`,
and collect the actual JPEG URLs or processed MP4 source. Shopify processes files
asynchronously and may transform image/video bytes, so local hashes bind the
reviewed upload request, not a claim that CDN bytes are identical. See
[Shopify's media documentation](https://shopify.dev/docs/apps/build/product-merchandising/products-and-collections/manage-media).

Return `upload-receipt.json` in this shape, with real values from that upload:

```json
{
  "experiment_id": "exp-dolphin-reel-v4",
  "platform": "instagram",
  "content_sha256": "COPY_FROM_UPLOAD_REQUEST",
  "verified_by": "ACTUAL_REVIEWER",
  "verified_at": "ACTUAL_ISO_TIME_WITH_OFFSET",
  "files": [
    {
      "path": "final/video.mp4",
      "sha256": "COPY_THIS_FILE_HASH_FROM_REQUEST",
      "file_id": "ACTUAL_gid://shopify/Video/ID",
      "file_status": "READY",
      "cdn_url": "ACTUAL_PROCESSED_SHOPIFY_MP4_URL"
    },
    {
      "path": "final/cover.jpg",
      "sha256": "COPY_THIS_FILE_HASH_FROM_REQUEST",
      "file_id": "ACTUAL_gid://shopify/MediaImage/ID",
      "file_status": "READY",
      "cdn_url": "ACTUAL_AMIEES_SHOPIFY_JPEG_URL"
    }
  ]
}
```

Placeholders above are instructions, not valid receipts. Every final asset must
appear once. The uploader attests to store, identity and READY status; this local
layer validates the receipt but does not independently query Shopify or prove
remote availability. Check the public CDN result before scheduling.

```powershell
python content_pipeline.py record-upload --package content/finished/exp-dolphin-reel-v4--instagram --receipt content/finished/exp-dolphin-reel-v4--instagram/upload-receipt.json
```

**For the approved Dolphin Reel:** package the existing
`output/nature-series/reels/dolphin-dreams-ring/dolphin-dreams-ring-reel-v4.mp4`
with its reference, final cover and caption. After upload recording, download the
actual MP4 from the registered Shopify CDN URL to `_pipeline/hosted-video.mp4`.
Watch that downloaded rendition and check the product, motion, overlay and audio.
Shopify may transcode it, so its hash is intentionally separate from the original
approved master. Record this explicit review:

```powershell
python content_pipeline.py review-hosted-video --package content/finished/exp-dolphin-reel-v4--instagram --file _pipeline/hosted-video.mp4 --reviewed-by Gabe
```

The command validates and hashes the local downloaded rendition. The reviewer
attests that it came from the recorded CDN URL and was watched. It does not
download it or prove that claim. The publisher downloads the exact registered
URL before container creation and requires the bytes to match this review.
Any new upload or hosted review invalidates the old queue draft and schedule
approval. Never overwrite a Shopify file already scheduled for publication.
See [INSTAGRAM-REELS.md](INSTAGRAM-REELS.md) for publishing and recovery details.
Pinterest video remains blocked by the current image-only RSS publisher.

## How approved content becomes scheduled

Use the package path for the reviewed image, carousel or Instagram Reel.
For Pinterest images, fill a board, unique title, description, exact product
destination and keywords first. Existing `variant` selections are retained; the
pipeline adds `utm_content=<experiment_id>` and a content-experiment campaign.

```powershell
python content_pipeline.py prepare-queue --package PATH_TO_PACKAGE --at "CHOSEN_ISO_TIME_WITH_OFFSET"
# Read _pipeline/queue-draft.json, checking every URL, caption, board and time.
python content_pipeline.py approve-schedule --package PATH_TO_PACKAGE --reviewed-by Gabe
# Run only after explicit approval to add this exact item to the publishing queue:
python content_pipeline.py apply-queue --package PATH_TO_PACKAGE --confirm-add-to-live-queue
```

The new slot must be at least one hour ahead, must not collide with a reserved
slot and must keep the existing Instagram 20-hour gap. The Nature Series already
occupies daily slots through December 17. Choose an actually free slot after the
series, or get a separate decision about schedule changes; this tool never moves
or replaces existing rows. It refuses unresolved overdue Instagram rows.

The queue draft includes a base queue hash. If another person changes that queue,
prepare and approve a fresh draft. Queue application only appends bytes; headers,
old CSV quoting, GUIDs, images and times remain intact. An interrupted registry
save can be retried without duplicating a matching queue ID. A conflicting ID is
an error, never an overwrite.

Inspect `git diff`, then commit and push the **registry and affected queue
together**, using a normal non-forced push. Do not run this concurrently with
another queue editor. A local append alone is not a remote GitHub schedule.

```powershell
git diff -- ig_queue.csv pins.csv content/experiments.json
git add content/experiments.json ig_queue.csv
# For a Pinterest item stage pins.csv instead of ig_queue.csv.
git commit -m "Schedule approved Amiees experiment"
git push
```

No existing CSV columns are added. Stable IDs (`ig-exp-<experiment_id>` and
`amiees-exp-<experiment_id>`) join the sidecar registry to the existing publishers.
Instagram state retains that queue ID as its key. The analytics collector adds
experiment metadata for new approved pipeline posts only; legacy posts and metric
requests remain unchanged. `ig_learning.py` and `ig_content_planner.py` keep reading
their existing inputs without any required schema change.

## Publication and measurement reconciliation

```powershell
python content_pipeline.py sync
```

This reads the queues, `ig_state.json` and `ig_analytics.json`. A matching Instagram
media ID and real collection snapshot advance the lifecycle. Zero counts remain
zero and missing values remain unknown. A changed/missing queue row is flagged
for reconciliation instead of being recreated. Inspect the actual published
creative before interpreting a controlled test: lineage alone cannot verify
what a legacy publisher ultimately displayed.

The new Content Pipeline workflow runs tests on relevant pushes/PRs and syncs
metadata daily at 15:47 UTC and on manual dispatch. Its main-branch job commits
only `content/experiments.json`, with non-forced retry after concurrent changes.
It never scans private local media, uploads, approves, applies queue drafts or
calls the publishing scripts. Existing publishing/analytics schedules are unchanged.

Pinterest RSS has no returned public Pin ID. After verifying a real Pin, supply
an explicit result receipt to `record-pin-result --item-id ID --receipt FILE`:

```json
{
  "experiment_id": "YOUR_EXPERIMENT_ID",
  "published_post_id": "ACTUAL_NUMERIC_PIN_ID",
  "permalink": "https://www.pinterest.com/pin/ACTUAL_NUMERIC_PIN_ID/",
  "verified_by": "ACTUAL_REVIEWER",
  "observed_at": "ACTUAL_ISO_TIME_WITH_OFFSET",
  "metrics": {"impressions": null, "outbound_clicks": null}
}
```

Omit `metrics` for publication-only confirmation. To mark measured, provide at
least one genuine observed count from Pinterest. There is no automatic Pinterest
analytics import in this layer, and entering null does not create a measurement.

## Tests and limits

```powershell
python -m unittest -v test_content_pipeline test_ig_content_planner test_ig_publish
```

Tests use isolated folders, real decoded JPEG fixtures, simulated Shopify receipts,
mocked video probes and mocked analytics calls. They do not prove a live upload or
publication. Current schedules/data stay unchanged by this build. Reviewers remain
responsible for product accuracy, rights, claims, audio, disclosure and the final
public post. No new content-generation workflow or paid service is introduced.
