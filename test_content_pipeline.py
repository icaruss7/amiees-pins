"""Isolated ingestion/lifecycle regressions. No live uploads or posts."""

import copy
import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import content_pipeline as cp
import content_assets as assets
import ig_analytics as analytics
import ig_pipeline_media as media
import ig_publish as publisher


NOW = datetime(2026, 10, 28, 20, tzinfo=timezone.utc)
SLOT = "2027-01-03T19:00:00-05:00"


class ContentPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(Path(__file__).resolve().parent))
        (self.root / "content").mkdir()
        self.write("content/experiments.json", {"schema_version": 1, "items": {}})
        self.write("ig_state.json", {"legacy": {"media_id": "10", "permalink": "https://www.instagram.com/p/legacy/", "posted_at": "2026-10-01T12:00:00+00:00"}})
        self.write("ig_analytics.json", {"posts": {}})
        self.write("ig_learning.json", {})
        self.write("ig_content_plan.json", {"start_date": "2026-10-29", "recommendations": [{
            "id": "test-2026-10-29-01", "product": "dolphin-dreams-ring", "format": "CAROUSEL",
            "content_angle": "detail_reveal", "hook": "Notice the tiny dolphin.",
            "hypothesis": "A clear detail may prompt a profile visit.", "objective": "Product consideration",
            "primary_success_metric": {"name": "profile_visits_per_reached_account"},
            "test_type": "controlled_follow_up", "control": {"reference_post_id": "legacy"}}]})
        self.csv("ig_queue.csv", ["post_id", "release_at", "product", "caption", "images", "status"], [
            {"post_id": "legacy", "release_at": "2026-10-01 12:00:00", "product": "dolphin-dreams-ring",
             "caption": "Original, quoted\ncaption", "images": "https://cdn.shopify.com/a.jpg", "status": "queued"},
            {"post_id": "ig-nature-reserved", "release_at": "2027-01-01 00:00:00", "product": "dolphin-dreams-ring",
             "caption": "Keep nature close.", "images": "https://cdn.shopify.com/b.jpg", "status": "queued"}])
        self.csv("pins.csv", ["guid", "board", "title", "description", "link", "image", "keywords", "release_at", "status"], [
            {"guid": "amiees-nature-old", "board": cp.BOARDS[0], "title": "Original Pin", "description": "Keep this.",
             "link": "https://amiees.com/products/dolphin-dreams-ring", "image": "https://cdn.shopify.com/a.jpg",
             "keywords": "nature", "release_at": "2027-01-01 17:00:00", "status": "queued"}])
        self.pipeline = cp.Pipeline(self.root, now=lambda: NOW)

    def tearDown(self):
        self.assertTrue(self.root.is_relative_to(Path(__file__).resolve().parent))
        self.temp.cleanup()

    def write(self, path, value):
        cp.write_json(self.root / path, value)

    def csv(self, path, headers, rows):
        with (self.root / path).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=headers, lineterminator="\r\n")
            writer.writeheader()
            writer.writerows(rows)

    def package(self, platform="instagram", format="IMAGE", experiment="exp-dolphin-v1"):
        package = self.pipeline.init(experiment, "dolphin-dreams-ring", platform, format)
        manifest = cp.load_json(package / "manifest.json")
        manifest.update(content_angle="worn_detail", hook="See the dolphin detail.",
                        hypothesis="Showing the actual detail may prompt a profile visit.", objective="Product consideration")
        manifest["pinterest"] = {"board": cp.BOARDS[0], "title": "Dolphin Ring", "description": "A small dolphin detail.",
                                  "destination_url": "https://amiees.com/products/dolphin-dreams-ring?variant=123", "keywords": ["dolphin ring"]}
        for final in manifest["final_assets"]:
            final["alt"] = "Dolphin ring worn on a hand."
            if final["role"] == "video":
                (package / final["path"]).write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\0" * 30)
            else:
                size = (1080, 1920) if final["role"] == "cover" else ((1000, 1500) if platform == "pinterest" else (1080, 1350))
                Image.new("RGB", size, "#efe9df").save(package / final["path"], "JPEG")
        Image.new("RGB", (600, 800), "white").save(package / "source/reference.jpg")
        (package / "caption.txt").write_text(manifest["hook"] + "\n\nKeep nature close.", encoding="utf-8")
        cp.write_json(package / "manifest.json", manifest)
        (package / "READY").write_text("Finished export", encoding="utf-8")
        return package

    def receipt(self, package):
        request = self.pipeline.upload_request(package)
        return {"experiment_id": request["experiment_id"], "platform": request["platform"],
                "content_sha256": request["content_sha256"], "verified_by": "Fixture reviewer",
                "verified_at": NOW.isoformat(), "files": [
                    {"path": item["path"], "sha256": item["sha256"], "file_status": "READY",
                     "file_id": "gid://shopify/" + ("Video" if item["role"] == "video" else "MediaImage") + "/123",
                     "cdn_url": ("https://cdn.shopify.com/videos/c/o/v/test.mp4" if item["role"] == "video" else
                                 "https://cdn.shopify.com/s/files/1/0677/3201/3371/files/" + Path(item["path"]).name)}
                    for item in request["files"]]}

    def uploaded(self, package):
        self.pipeline.ingest(package)
        self.pipeline.approve(package, "Fixture reviewer")
        return self.pipeline.record_upload(package, self.receipt(package))

    def schedule(self, package):
        self.uploaded(package)
        self.pipeline.prepare_queue(package, SLOT)
        self.pipeline.approve_schedule(package, "Fixture reviewer")
        return self.pipeline.apply_queue(package, confirm=True)

    def test_scaffold_from_planner_preserves_experiment_and_control(self):
        package = self.pipeline.init(from_plan="test-2026-10-29-01")
        manifest = cp.load_json(package / "manifest.json")
        self.assertEqual(manifest["experiment_id"], "test-2026-10-29-01")
        self.assertEqual(manifest["planner_reference"]["control"]["reference_post_id"], "legacy")
        self.assertEqual(manifest["hook"], "Notice the tiny dolphin.")
        item = cp.load_json(self.root / "content/experiments.json")["items"][cp.item_id(manifest)]
        self.assertEqual(item["publishing_status"], "needs_content")

    def test_new_verified_product_does_not_need_a_dummy_live_queue_row(self):
        before = (self.root / "ig_queue.csv").read_bytes()
        with self.assertRaises(cp.PipelineError):
            self.pipeline.init("exp-new-v1", "new-ring")
        self.pipeline.register_product("new-ring", "https://amiees.com/products/new-ring", "Gabe")
        package = self.pipeline.init("exp-new-v1", "new-ring")
        self.assertTrue((package / "manifest.json").exists())
        self.assertEqual((self.root / "ig_queue.csv").read_bytes(), before)

    def test_pinterest_can_seed_the_same_planner_experiment_without_a_carousel(self):
        package = self.pipeline.init(from_plan="test-2026-10-29-01", platform="pinterest")
        manifest = cp.load_json(package / "manifest.json")
        self.assertEqual(manifest["format"], "IMAGE")
        self.assertTrue(manifest["planner_reference"]["platform_adaptation_required"])

    def test_uploaded_unscheduled_item_does_not_advance_on_sync(self):
        package = self.package()
        self.uploaded(package)
        item = self.pipeline.sync()["items"][package.name]
        self.assertEqual(item["publishing_status"], "uploaded")
        self.assertNotIn("queue_observation", item)

    def test_scan_automates_validation_but_not_approval(self):
        package = self.package()
        before = {name: (self.root / name).read_bytes() for name in ("ig_queue.csv", "pins.csv")}
        result = self.pipeline.scan(package.parent)
        self.assertEqual(result[0]["status"], "ready_for_review")
        item = cp.load_json(self.root / "content/experiments.json")["items"][package.name]
        self.assertIsNone(item["approval"])
        self.assertTrue(item["final_assets"][0]["sha256"])
        for name, raw in before.items():
            self.assertEqual((self.root / name).read_bytes(), raw)

    def test_ready_marker_missing_files_and_bad_images_stay_unready(self):
        package = self.package()
        (package / "READY").unlink()
        self.assertEqual(self.pipeline.ingest(package)["publishing_status"], "needs_content")
        (package / "READY").touch()
        (package / "final/01.jpg").write_bytes(b"not a jpeg")
        item = self.pipeline.ingest(package)
        self.assertEqual(item["publishing_status"], "needs_content")
        with self.assertRaises(cp.PipelineError):
            self.pipeline.approve(package, "Gabe")

    def test_caption_or_asset_change_invalidates_approval_and_receipt(self):
        package = self.package()
        self.uploaded(package)
        (package / "caption.txt").write_text("See the dolphin detail.\n\nChanged caption.", encoding="utf-8")
        with self.assertRaises(cp.PipelineError):
            self.pipeline.prepare_queue(package, SLOT)
        item = self.pipeline.ingest(package)
        self.assertEqual(item["publishing_status"], "ready_for_review")
        self.assertIsNone(item["approval"])
        self.assertEqual(item["shopify_cdn_urls"], [])
        self.assertTrue(item["revisions"][-1]["approval"])

    def test_replacing_an_uploaded_image_revokes_its_approval(self):
        package = self.package()
        self.uploaded(package)
        Image.new("RGB", (1080, 1350), "blue").save(package / "final/01.jpg")
        with self.assertRaises(cp.PipelineError): self.pipeline.upload_request(package)
        item = self.pipeline.ingest(package)
        self.assertEqual(item["publishing_status"], "ready_for_review")
        self.assertIsNone(item["approval"])
        self.assertEqual(item["shopify_files"], [])

    def test_path_traversal_and_duplicate_paths_rejected(self):
        package = self.package()
        manifest = cp.load_json(package / "manifest.json")
        manifest["source_assets"] = ["../outside.jpg"]
        cp.write_json(package / "manifest.json", manifest)
        self.assertEqual(self.pipeline.ingest(package)["publishing_status"], "needs_content")
        manifest["source_assets"] = ["source/reference.jpg", "source/reference.jpg"]
        with self.assertRaises(cp.PipelineError):
            cp.validate_manifest(manifest)

    def test_receipt_requires_exact_revision_ready_ids_and_correct_host(self):
        package = self.package()
        self.pipeline.ingest(package)
        self.pipeline.approve(package, "Gabe")
        receipt = self.receipt(package)
        for change in ("hash", "processing", "host", "other_revision"):
            bad = copy.deepcopy(receipt)
            if change == "hash": bad["files"][0]["sha256"] = "0" * 64
            if change == "processing": bad["files"][0]["file_status"] = "PROCESSING"
            if change == "host": bad["files"][0]["cdn_url"] = "https://cdn.shopify.com.evil.example/a.jpg"
            if change == "other_revision": bad["content_sha256"] = "0" * 64
            with self.assertRaises((cp.PipelineError, ValueError)):
                self.pipeline.record_upload(package, bad)
        self.assertEqual(self.pipeline.record_upload(package, receipt)["publishing_status"], "uploaded")

    def test_upload_request_is_not_upload_and_preserves_file_bytes(self):
        package = self.package()
        self.pipeline.ingest(package)
        self.pipeline.approve(package, "Gabe")
        before = (package / "final/01.jpg").read_bytes()
        request = self.pipeline.upload_request(package)
        self.assertFalse(request["upload_automated"])
        self.assertIn(request["files"][0]["sha256"][:12], request["files"][0]["upload_filename"])
        self.assertEqual((package / "final/01.jpg").read_bytes(), before)

    def test_two_approvals_and_confirm_are_required_before_queue_append(self):
        package = self.package()
        self.uploaded(package)
        original = (self.root / "ig_queue.csv").read_bytes()
        self.pipeline.prepare_queue(package, SLOT)
        with self.assertRaises(cp.PipelineError): self.pipeline.apply_queue(package, True)
        self.pipeline.approve_schedule(package, "Gabe")
        with self.assertRaises(cp.PipelineError): self.pipeline.apply_queue(package)
        self.assertEqual((self.root / "ig_queue.csv").read_bytes(), original)
        self.assertEqual(self.pipeline.apply_queue(package, True)["publishing_status"], "scheduled")
        self.assertTrue((self.root / "ig_queue.csv").read_bytes().startswith(original))

    def test_queue_append_is_idempotent_and_preserves_multiline_csv(self):
        package = self.package(format="CAROUSEL")
        pin_before = (self.root / "pins.csv").read_bytes()
        old = cp.read_queue(self.root / "ig_queue.csv")[2]
        item = self.schedule(package)
        first = (self.root / "ig_queue.csv").read_bytes()
        self.pipeline.apply_queue(package, True)
        self.assertEqual((self.root / "ig_queue.csv").read_bytes(), first)
        rows = cp.read_queue(self.root / "ig_queue.csv")[2]
        self.assertEqual(rows[:-1], old)
        self.assertEqual(rows[-1]["post_id"], "ig-exp-exp-dolphin-v1")
        self.assertEqual(rows[-1]["images"].count("|"), 1)
        self.assertEqual(item["experiment_id"], "exp-dolphin-v1")
        self.assertEqual((self.root / "pins.csv").read_bytes(), pin_before)

    def test_changed_queue_or_tampered_draft_needs_new_review(self):
        package = self.package()
        self.uploaded(package)
        self.pipeline.prepare_queue(package, SLOT)
        self.pipeline.approve_schedule(package, "Gabe")
        with (self.root / "ig_queue.csv").open("ab") as stream: stream.write(b"\r\n")
        before = (self.root / "ig_queue.csv").read_bytes()
        with self.assertRaises(cp.PipelineError): self.pipeline.apply_queue(package, True)
        self.assertEqual((self.root / "ig_queue.csv").read_bytes(), before)
        self.pipeline.prepare_queue(package, SLOT)
        self.pipeline.approve_schedule(package, "Gabe")
        draft = cp.load_json(package / "_pipeline/queue-draft.json")
        draft["row"]["caption"] = "Unreviewed edit"
        cp.write_json(package / "_pipeline/queue-draft.json", draft)
        with self.assertRaises(cp.PipelineError): self.pipeline.apply_queue(package, True)

    def test_retry_after_queue_write_registry_failure_does_not_duplicate(self):
        package = self.package()
        self.uploaded(package)
        self.pipeline.prepare_queue(package, SLOT)
        self.pipeline.approve_schedule(package, "Gabe")
        with patch.object(cp, "write_json", side_effect=OSError("simulated registry write failure")):
            with self.assertRaises(OSError): self.pipeline.apply_queue(package, True)
        self.assertEqual(len(cp.read_queue(self.root / "ig_queue.csv")[2]), 3)
        self.pipeline.apply_queue(package, True)
        self.assertEqual(len(cp.read_queue(self.root / "ig_queue.csv")[2]), 3)

    def test_nature_schedule_collision_and_immediate_or_naive_time_rejected(self):
        package = self.package()
        self.uploaded(package)
        for value in ("2027-01-01T04:00:00+00:00", "2026-10-28T20:30:00+00:00", "2027-01-03T19:00:00"):
            with self.assertRaises(cp.PipelineError): self.pipeline.prepare_queue(package, value)

    def test_scheduled_assets_cannot_be_revised_in_place(self):
        package = self.package()
        self.schedule(package)
        before = (self.root / "ig_queue.csv").read_bytes()
        Image.new("RGB", (1080, 1350), "blue").save(package / "final/01.jpg")
        with self.assertRaises(cp.PipelineError): self.pipeline.ingest(package)
        self.assertEqual((self.root / "ig_queue.csv").read_bytes(), before)

    def test_pinterest_row_preserves_variant_and_utm_experiment(self):
        package = self.package(platform="pinterest")
        item = self.schedule(package)
        row = cp.read_queue(self.root / "pins.csv")[2][-1]
        self.assertIn("variant=123", row["link"])
        self.assertIn("utm_content=exp-dolphin-v1", row["link"])
        self.assertEqual(row["guid"], item["queue_id"])

    def test_rss_release_never_claims_public_pin_or_measurement(self):
        package = self.package(platform="pinterest")
        self.schedule(package)
        self.pipeline.clock = lambda: datetime(2027, 1, 4, tzinfo=timezone.utc)
        item = self.pipeline.sync()["items"][package.name]
        self.assertTrue(item["feed_release_eligible"])
        self.assertEqual(item["publishing_status"], "scheduled")
        self.assertIsNone(item["published_post_id"])

    def test_pinterest_publication_requires_an_explicit_result_receipt(self):
        package = self.package(platform="pinterest")
        self.schedule(package)
        self.pipeline.clock = lambda: datetime(2027, 1, 4, 12, tzinfo=timezone.utc)
        receipt = self.root / "pin-result.json"
        cp.write_json(receipt, {"experiment_id": "exp-dolphin-v1", "published_post_id": "12345",
                               "permalink": "https://www.pinterest.com/pin/12345/", "verified_by": "Gabe",
                               "observed_at": "2027-01-04T10:00:00+00:00", "metrics": {"impressions": 0, "outbound_clicks": None}})
        item = self.pipeline.record_pin_result(package.name, receipt)
        self.assertEqual(item["publishing_status"], "measured")
        self.assertIsNone(item["analytics_linkage"]["latest"]["outbound_clicks"])

    def test_instagram_publication_and_measurement_link_by_stable_queue_id(self):
        package = self.package()
        item = self.schedule(package)
        key = item["queue_id"]
        self.write("ig_state.json", {key: {"media_id": "123", "posted_at": "2027-01-04T00:00:00+00:00",
                                                "permalink": "https://www.instagram.com/p/test/"}})
        self.pipeline.clock = lambda: datetime(2027, 1, 4, 12, tzinfo=timezone.utc)
        self.assertEqual(self.pipeline.sync()["items"][package.name]["publishing_status"], "published")
        self.write("ig_analytics.json", {"posts": {key: {"media_id": "123", "latest": {"reach": 0, "shares": None},
                                                               "history": [{"collected_at": "2027-01-04T10:00:00+00:00"}]}}})
        measured = self.pipeline.sync()["items"][package.name]
        self.assertEqual(measured["publishing_status"], "measured")
        self.assertEqual(measured["analytics_linkage"]["experiment_id"], "exp-dolphin-v1")
        self.assertIsNone(measured["analytics_linkage"]["latest"]["shares"])

    def test_analytics_sidecar_enrichment_is_additive_and_ignores_legacy(self):
        package = self.package()
        item = self.schedule(package)
        registry = cp.load_json(self.root / "content/experiments.json")
        metadata = analytics.experiment_metadata(item["queue_id"], item["product"], registry)
        self.assertEqual(metadata["experiment_id"], "exp-dolphin-v1")
        self.assertEqual(analytics.experiment_metadata("legacy", item["product"], registry), {})
        self.assertEqual(analytics.experiment_metadata(item["queue_id"], "different-product", registry), {})
        self.assertEqual(analytics.experiment_metadata(item["queue_id"], item["product"], {"items": []}), {})

    def test_existing_analytics_collector_still_collects_and_adds_lineage(self):
        package = self.package()
        item = self.schedule(package)
        key = item["queue_id"]
        state = cp.load_json(self.root / "ig_state.json")
        state[key] = {"media_id": "123", "posted_at": "2026-10-28T12:00:00+00:00", "permalink": "https://www.instagram.com/p/test/"}
        self.write("ig_state.json", state)
        with patch.multiple(analytics, HERE=str(self.root), STATE=str(self.root / "ig_state.json"),
                            QUEUE=str(self.root / "ig_queue.csv"), OUT=str(self.root / "ig_analytics.json"), TOKEN="fixture-only"), \
                patch.object(analytics, "find_ig_user", return_value=("fixture", "amiees.com_")), \
                patch.object(analytics, "fetch_account_profile", return_value={"followers_count": 31}), \
                patch.object(analytics, "fetch_recent_media", return_value=({}, {})), \
                patch.object(analytics, "fetch_media_metadata", return_value={"media_type": "IMAGE", "media_product_type": "FEED"}), \
                patch.object(analytics, "fetch_metric_batch", return_value={"reach": 5, "views": 10, "shares": 0}), \
                patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(analytics.main(), 0)
        result = cp.load_json(self.root / "ig_analytics.json")["posts"]
        self.assertEqual(result[key]["experiment_id"], "exp-dolphin-v1")
        self.assertEqual(result[key]["latest"]["reach"], 5)
        self.assertNotIn("experiment_id", result["legacy"])

    def test_reel_needs_a_separate_hosted_rendition_review(self):
        package = self.package(format="REELS")
        info = {"media_type": "MP4", "codec": "h264", "width": 1080, "height": 1920,
                "duration_seconds": 7.5, "audio_codecs": []}
        with patch.object(assets, "inspect_video", return_value=info):
            self.uploaded(package)
            with self.assertRaisesRegex(cp.PipelineError, "Shopify-hosted"):
                self.pipeline.prepare_queue(package, SLOT)
        self.assertEqual(len(cp.read_queue(self.root / "ig_queue.csv")[2]), 2)

    def reel(self, manifest_fields=None):
        package = self.package(format="REELS")
        if manifest_fields:
            manifest = cp.load_json(package / "manifest.json")
            manifest.update(manifest_fields)
            cp.write_json(package / "manifest.json", manifest)
        info = {"media_type": "MP4", "codec": "h264", "width": 1080, "height": 1920,
                "duration_seconds": 7.5, "audio_codecs": []}
        with patch.object(assets, "inspect_video", return_value=info), patch.object(cp, "inspect_video", return_value=info):
            self.uploaded(package)
            # Simulate a processed rendition that differs from the original master.
            (package / "_pipeline/hosted-video.mp4").write_bytes(b"processed CDN fixture")
            self.pipeline.review_hosted_video(package, "_pipeline/hosted-video.mp4", "Fixture reviewer")
            draft = self.pipeline.prepare_queue(package, SLOT)
            self.pipeline.approve_schedule(package, "Fixture reviewer")
            self.pipeline.apply_queue(package, confirm=True)
        return package, draft

    def test_native_audio_is_bound_to_separate_schedule_approval(self):
        audio = {"configuration": {"audio_id": "123", "audio_volume": 70, "video_volume": 1},
                 "title": "Fixture", "artist": "Fixture artist", "catalog_collected_at": NOW.isoformat(),
                 "rights_review": {"commercial_use_verified": True, "by": "Fixture reviewer",
                                   "at": NOW.isoformat(), "source_url": "https://www.facebook.com/sound/collection/"}}
        _, draft = self.reel({"instagram_audio": audio, "is_ai_generated": True, "schedule_mode": "additional_reel"})
        registry = cp.load_json(self.root / "content/experiments.json")
        resolved = media.resolve_row(draft["row"], registry)
        self.assertEqual(resolved["audio_configuration"], audio["configuration"])
        self.assertTrue(resolved["is_ai_generated"])
        record = next(iter(registry["items"].values()))
        record["instagram_audio"]["configuration"]["audio_id"] = "456"
        with self.assertRaisesRegex(ValueError, "approved queue draft"):
            media.resolve_row(draft["row"], registry)

    def test_extra_slot_never_moves_nature_and_blocks_uncertain_overdue_posts(self):
        before = (self.root / "ig_queue.csv").read_bytes()
        rows = cp.read_queue(self.root / "ig_queue.csv")[2]
        slot = cp.iso("2027-01-01T09:00:00Z")
        with self.assertRaises(cp.PipelineError):
            self.pipeline.check_time("instagram", rows, slot, "extra")
        self.pipeline.check_time("instagram", rows, slot, "extra", additional=True)
        self.assertEqual((self.root / "ig_queue.csv").read_bytes(), before)
        overdue = [{"post_id": "overdue", "status": "queued", "release_at": "2026-10-28 18:00:00"}]
        future = cp.iso("2026-10-29T09:00:00Z")
        self.pipeline.check_time("instagram", overdue, future, "extra", additional=True)
        self.write("ig_state.json", {"overdue": {"phase": "publishing"}})
        with self.assertRaisesRegex(cp.PipelineError, "overdue"):
            self.pipeline.check_time("instagram", overdue, future, "extra", additional=True)

    def test_reel_pipeline_to_publisher_and_analytics_with_old_queue_schema(self):
        before = (self.root / "ig_queue.csv").read_bytes()
        package, draft = self.reel()
        self.assertTrue((self.root / "ig_queue.csv").read_bytes().startswith(before))
        registry = cp.load_json(self.root / "content/experiments.json")
        record = next(iter(registry["items"].values()))
        self.assertNotEqual(record["hosted_video_review"]["sha256"], record["final_assets"][0]["sha256"])
        row = media.resolve_row(draft["row"], registry)
        self.assertEqual(row["media_type"], "REELS")
        self.assertEqual(row["images"], "")
        calls, state = [], {}
        def api(method, path, **params):
            calls.append((path, params))
            if path.endswith("/media_publish"):
                return {"id": "published-reel"}
            if params.get("fields") == "permalink":
                return {"permalink": "https://www.instagram.com/reel/fixture/"}
            return {"id": "prepared-reel", "status_code": "FINISHED"}
        with patch.object(publisher, "call", side_effect=api), patch.object(publisher, "checkpoint"), patch.object(publisher, "verify_video") as verify:
            publisher.publish("account", row, state)
            publisher.publish("account", row, state)
        verify.assert_called_once_with(row)
        self.assertEqual(sum(p.endswith("/media_publish") for p, _ in calls), 1)
        creation = next(params for path, params in calls if path.endswith("/media"))
        self.assertEqual(creation["cover_url"], draft["media"]["cover_url"])
        linkage = analytics.experiment_metadata(record["queue_id"], record["product"], registry)
        self.assertEqual(linkage["experiment_id"], record["experiment_id"])
        # Reconcile actual-shaped publication and measurement fixtures.
        state[record["queue_id"]]["posted_at"] = "2026-10-28T19:00:00+00:00"
        self.write("ig_state.json", state)
        self.write("ig_analytics.json", {"posts": {record["queue_id"]: {
            "media_id": "published-reel", "latest": {"reach": 6},
            "history": [{"collected_at": NOW.isoformat()}]}}})
        measured = self.pipeline.sync()["items"][record["pipeline_item_id"]]
        self.assertEqual(measured["publishing_status"], "measured")
        self.assertEqual(measured["analytics_linkage"]["experiment_id"], record["experiment_id"])

    def test_reel_registry_tampering_cannot_reach_meta(self):
        _, draft = self.reel()
        registry = cp.load_json(self.root / "content/experiments.json")
        mutations = [lambda r: r.update(publishing_status="uploaded"),
                     lambda r: r["approval"].update(content_sha256="changed"),
                     lambda r: r["queue_draft"]["media"].update(video_url="https://other.example/evil.mp4"),
                     lambda r: r["hosted_video_review"].update(sha256="f" * 64),
                     lambda r: r["upload_receipt"].update(verified_by="changed"),
                     lambda r: r["schedule_approval"].update(draft_sha256="changed")]
        for change in mutations:
            modified = copy.deepcopy(registry)
            change(next(iter(modified["items"].values())))
            with self.assertRaises(ValueError):
                media.resolve_row(draft["row"], modified)
        with self.assertRaises(ValueError):
            media.resolve_row({**draft["row"], "caption": "changed"}, registry)
        with self.assertRaises(ValueError):
            media.resolve_row(draft["row"], {})

    def test_new_upload_invalidates_hosted_review_and_schedule_draft(self):
        package = self.package(format="REELS")
        info = {"media_type": "MP4", "codec": "h264", "width": 1080, "height": 1920,
                "duration_seconds": 7.5, "audio_codecs": []}
        with patch.object(assets, "inspect_video", return_value=info), patch.object(cp, "inspect_video", return_value=info):
            self.uploaded(package)
            self.pipeline.review_hosted_video(package, "final/video.mp4", "Fixture reviewer")
            self.pipeline.prepare_queue(package, SLOT)
            self.pipeline.approve_schedule(package, "Fixture reviewer")
            record = self.pipeline.record_upload(package, self.receipt(package))
            self.assertIsNone(record["hosted_video_review"])
            self.assertIsNone(record["queue_draft"])
            self.assertIsNone(record["schedule_approval"])

    def test_pinterest_video_still_cannot_enter_image_feed(self):
        package = self.package(platform="pinterest", format="VIDEO")
        with patch.object(assets, "inspect_video", return_value={"media_type": "MP4"}):
            self.uploaded(package)
            with self.assertRaisesRegex(cp.PipelineError, "Pinterest video"):
                self.pipeline.prepare_queue(package, SLOT)

    def test_ffprobe_is_required_and_uses_local_only_protocols(self):
        package = self.package(format="REELS")
        with patch.object(assets.shutil, "which", return_value=None), patch.dict(assets.os.environ, {"AMIEES_FFPROBE": ""}):
            with self.assertRaisesRegex(cp.PipelineError, "ffprobe"):
                assets.inspect_video(package / "final/video.mp4")
        output = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920}],
                  "format": {"duration": "7.5"}}
        with patch.object(assets.subprocess, "run") as run:
            run.return_value.stdout = json.dumps(output)
            assets.inspect_video(package / "final/video.mp4", "trusted-ffprobe")
            self.assertIn("file,pipe", run.call_args.args[0])

    def test_registry_lock_and_existing_package_fail_without_overwrite(self):
        package = self.package()
        before = (package / "manifest.json").read_bytes()
        with self.assertRaises(cp.PipelineError):
            self.pipeline.init("exp-dolphin-v1", "dolphin-dreams-ring")
        self.assertEqual((package / "manifest.json").read_bytes(), before)
        (self.root / "content/.pipeline.lock").touch()
        with self.assertRaises(cp.PipelineError): self.pipeline.ingest(package)
        (self.root / "content/.pipeline.lock").unlink()


if __name__ == "__main__":
    unittest.main()
