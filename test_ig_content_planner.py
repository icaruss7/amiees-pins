"""Safety and inference regressions. No API calls or live publication."""

import copy
import csv
import hashlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import ig_content_planner as planner


START = date(2026, 10, 29)
COLLECTED = "2026-10-28T19:15:58+00:00"


def queued(product, when="2026-12-15 00:00:00", **extra):
    return {"post_id": "queued-" + product, "release_at": when, "product": product,
            "caption": "A nature-inspired design.", "images": "https://example.com/one.jpg|https://example.com/two.jpg",
            "status": "queued", **extra}


def post(product, reach=6, actions=1, **extra):
    item = {"product": product, "post_id": "published-" + product, "media_id": "media-" + product,
            "posted_at": "2026-10-15T15:00:00+00:00", "media_type": "CAROUSEL_ALBUM",
            "caption": "Original opening about the actual design.",
            "history": [{"collected_at": COLLECTED}],
            "latest": {"reach": reach, "views": 20, "likes": 3, "comments": actions,
                       "saved": actions, "shares": actions, "profile_visits": actions,
                       "follows": 0, "total_interactions": 4}}
    item.update(extra)
    return item


def fixture():
    queue = [queued("design-" + str(i)) for i in range(12)]
    posts = {"published-design-" + str(i): post("design-" + str(i)) for i in range(8)}
    analytics = {"last_run": {"collected_at": COLLECTED, "posts_skipped": 0}, "posts": posts}
    learning = {"sample_size": 8, "data_notes": {"collected_at": COLLECTED},
                "benchmarks": {"median_reach_or_views": 6},
                "recommendations": ["Strongest current winners: design-0. Spend more immediately."]}
    return learning, analytics, queue


class PlannerTests(unittest.TestCase):
    def build(self, learning=None, analytics=None, queue=None, start=START, state=None):
        base = fixture()
        return planner.build_plan(learning if learning is not None else base[0],
                                  analytics if analytics is not None else base[1],
                                  queue if queue is not None else base[2], start, state)

    def test_full_seven_day_contract(self):
        result = self.build()
        self.assertEqual(len(result["days"]), 7)
        self.assertEqual(len(result["recommendations"]), 7)
        self.assertEqual(result["end_date"], "2026-11-04")
        fields = {"product", "content_angle", "hook", "format", "objective", "hypothesis",
                  "primary_success_metric", "confidence_level"}
        for row in result["recommendations"]:
            self.assertTrue(fields <= row.keys())
            self.assertEqual(row["status"], "recommendation_only_requires_review")
        self.assertFalse(result["safety"]["queue_writes"])
        self.assertFalse(result["safety"]["auto_publish"])

    def test_tiny_denominators_never_inherit_winner_claims(self):
        learning, analytics, queue = fixture()
        for item in analytics["posts"].values():
            item["latest"].update(reach=1, shares=1, saved=1, profile_visits=1)
        result = self.build(learning, analytics, queue)
        self.assertEqual({r["confidence_level"] for r in result["recommendations"]}, {"low"})
        self.assertNotIn("Spend more immediately", json.dumps(result))
        self.assertIn("Promising signal, needs another test", json.dumps(result))

    def test_future_nature_and_recent_products_reserved_even_for_followups(self):
        learning, analytics, queue = fixture()
        queue[0].update(post_id="ig-nature-01-design-0", release_at="2026-10-31 23:00:00")
        # Also block 14 days AFTER the end of the proposed week.
        queue[1]["release_at"] = "2026-11-18 23:59:59"
        analytics["posts"]["published-design-2"]["posted_at"] = "2026-10-27T20:00:00+00:00"
        result = self.build(learning, analytics, queue)
        selected = {r["product"] for r in result["recommendations"]}
        self.assertTrue({"design-0", "design-1", "design-2"}.isdisjoint(selected))
        self.assertEqual(result["deduplication"]["protected_end"], "2026-11-18")

    def test_overdue_uncollected_queue_is_reserved(self):
        learning, analytics, queue = fixture()
        queue[0]["release_at"] = "2026-09-01 12:00:00"
        result = self.build(learning, analytics, queue)
        self.assertNotIn("design-0", [r["product"] for r in result["recommendations"]])

    def test_publication_state_resolves_overdue_backlog(self):
        learning, analytics, queue = fixture()
        queue[0]["release_at"] = "2026-09-01 12:00:00"
        state = {queue[0]["post_id"]: {"media_id": "published-already"}}
        result = self.build(learning, analytics, queue, state=state)
        self.assertNotIn("design-0", result["deduplication"]["protected_products"])

    def test_scheduled_concept_reserves_entire_test_category(self):
        learning, analytics, queue = fixture()
        queue.append(queued("reserved-piece", when="2026-10-30 23:00:00", content_angle="gift_identity"))
        result = self.build(learning, analytics, queue)
        self.assertNotIn("gift_identity", [r["content_angle"] for r in result["recommendations"]])
        self.assertEqual(len(result["recommendations"]), 6)

    def test_scheduled_hook_collision_moves_to_another_product(self):
        learning, analytics, queue = fixture()
        original = self.build(learning, analytics, queue)["recommendations"][0]
        queue.append(queued("reserved-piece", when="2026-10-30 23:00:00", hook=original["hook"], concept="custom"))
        result = self.build(learning, analytics, queue)
        self.assertNotIn(original["hook"], [r["hook"] for r in result["recommendations"]])

    def test_daylight_saving_queue_dates_remain_local(self):
        queue = [queued("before-clock", "2026-10-31 23:00:00", post_id="ig-nature-before"),
                 queued("after-clock", "2026-11-02 00:00:00", post_id="ig-nature-after")]
        result = self.build(queue=queue)
        before = result["days"][2]["scheduled_posts"][0]
        after = result["days"][3]["scheduled_posts"][0]
        self.assertEqual(before["date"], "2026-10-31")
        self.assertTrue(before["release_at_local"].endswith("19:00:00-04:00"))
        self.assertEqual(after["date"], "2026-11-01")
        self.assertTrue(after["release_at_local"].endswith("19:00:00-05:00"))

    def test_daily_snapshots_and_duplicate_media_do_not_inflate_samples(self):
        learning, analytics, queue = fixture()
        analytics["posts"] = {"one": post("design-0")}
        analytics["posts"]["one"]["history"] *= 90
        analytics["posts"]["duplicate"] = copy.deepcopy(analytics["posts"]["one"])
        result = self.build(learning, analytics, queue)
        self.assertEqual(result["data_summary"]["distinct_posts"], 1)
        self.assertEqual(result["data_summary"]["mature_posts"], 1)
        self.assertEqual({r["confidence_level"] for r in result["recommendations"]}, {"low"})

    def test_immature_and_future_snapshots_are_not_comparators(self):
        learning, analytics, queue = fixture()
        for item in analytics["posts"].values():
            item["posted_at"] = "2026-10-28T10:00:00+00:00"
        self.assertEqual(self.build(learning, analytics, queue)["data_summary"]["mature_posts"], 0)
        analytics["posts"]["published-design-0"].update(posted_at="2026-10-01T00:00:00+00:00",
                                                        history=[{"collected_at": "2026-11-28T00:00:00+00:00"}])
        self.assertEqual(self.build(learning, analytics, queue)["data_summary"]["mature_posts"], 0)

    def test_unknown_counts_are_not_zero_and_views_are_not_reach(self):
        learning, analytics, queue = fixture()
        for item in analytics["posts"].values():
            item["latest"] = {"reach": None, "views": 100000, "saved": None}
        result = self.build(learning, analytics, queue)
        self.assertIsNone(result["data_summary"]["median_reach"])
        self.assertEqual(result["data_summary"]["metric_availability_posts"]["saved"], 0)
        self.assertEqual({r["confidence_level"] for r in result["recommendations"]}, {"low"})
        self.assertEqual({r["primary_success_metric"]["name"] for r in result["recommendations"]}, {"reach"})

    def test_real_zeros_remain_zero(self):
        item = planner.normalize_posts({"posts": {"one": post("design-0", actions=0)}}, [])[0]
        self.assertEqual(item["metrics"]["saved"], 0)
        self.assertEqual(item["metrics"]["shares"], 0)

    def test_stale_data_caps_confidence(self):
        result = self.build(start=date(2026, 11, 3))
        self.assertFalse(result["data_summary"]["analytics_fresh"])
        self.assertEqual({r["confidence_level"] for r in result["recommendations"]}, {"low"})

    def test_controlled_followup_records_one_variable_and_matched_age_limit(self):
        rows = [r for r in self.build()["recommendations"] if r["test_type"] == "controlled_follow_up"]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["control"]["change_only"], "opening_caption_line")
            self.assertIn("matched-age", row["control"]["comparison_limit"])
            self.assertNotEqual(row["hook"], row["control"]["reference_hook"])

    def test_held_only_products_are_not_reintroduced(self):
        learning, analytics, queue = fixture()
        queue[0]["status"] = "held"
        result = self.build(learning, analytics, queue)
        self.assertNotIn("design-0", [r["product"] for r in result["recommendations"]])

    def test_no_safe_inventory_yields_observation_days(self):
        queue = [queued("reserved-piece", when="2026-10-30 23:00:00")]
        result = self.build(queue=queue)
        self.assertEqual(len(result["days"]), 7)
        self.assertEqual(result["recommendations"], [])
        self.assertTrue(result["warnings"])

    def test_input_objects_unchanged_and_output_deterministic(self):
        learning, analytics, queue = fixture()
        before = copy.deepcopy((learning, analytics, queue))
        first = self.build(learning, analytics, queue)
        second = self.build(learning, analytics, list(reversed(queue)))
        self.assertEqual(first, second)
        self.assertEqual((learning, analytics, queue), before)
        self.assertEqual(planner.render_markdown(first), planner.render_markdown(second))

    def test_medium_needs_independent_sufficient_mature_posts_never_high(self):
        items = {str(i): post("design-0", reach=150, actions=5, media_id=str(i)) for i in range(3)}
        normalized = planner.normalize_posts({"posts": items, "last_run": {"collected_at": COLLECTED}}, [])
        self.assertEqual(planner.evidence(normalized, "saved", "CAROUSEL", True)["confidence_level"], "medium")
        items["2"]["latest"]["reach"] = 2
        normalized = planner.normalize_posts({"posts": items, "last_run": {"collected_at": COLLECTED}}, [])
        self.assertEqual(planner.evidence(normalized, "saved", "CAROUSEL", True)["confidence_level"], "low")
        self.assertEqual(planner.evidence(normalized, "saved", "CAROUSEL", False)["confidence_level"], "low")

    def test_old_cached_snapshots_do_not_gain_confidence_from_a_new_run(self):
        items = {str(i): post("design-0", reach=500, actions=50, media_id=str(i),
                             history=[{"collected_at": "2026-10-20T19:00:00+00:00"}]) for i in range(3)}
        normalized = planner.normalize_posts({"posts": items, "last_run": {"collected_at": COLLECTED}}, [])
        self.assertEqual(planner.evidence(normalized, "saved", "CAROUSEL", True)["confidence_level"], "low")

    def test_future_publication_cannot_free_a_scheduled_product(self):
        learning, analytics, queue = fixture()
        queue[0].update(post_id="future-claim", release_at="2026-10-30 23:00:00")
        analytics["posts"]["future-claim"] = post("design-0", posted_at="2026-10-30T23:00:00+00:00")
        result = self.build(learning, analytics, queue)
        self.assertIn("design-0", result["deduplication"]["protected_products"])
        self.assertNotIn("design-0", [r["product"] for r in result["recommendations"]])

    def test_invalid_queued_dates_and_duplicate_ids_fail_closed(self):
        for queue in ([queued("design-0", when="bad")], [queued("design-0"), queued("design-0")]):
            with self.assertRaises(ValueError):
                self.build(queue=queue)

    def test_empty_analytics_produces_low_confidence_exploration(self):
        result = self.build(learning={}, analytics={})
        self.assertEqual(len(result["recommendations"]), 7)
        self.assertEqual({r["confidence_level"] for r in result["recommendations"]}, {"low"})
        self.assertEqual({r["test_type"] for r in result["recommendations"]}, {"exploratory_test"})

    def test_outputs_do_not_contain_em_dashes(self):
        result = self.build()
        self.assertNotIn("\u2014", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("\u2014", planner.render_markdown(result))

    def test_cli_preserves_all_inputs_and_is_offline_and_repeatable(self):
        learning, analytics, queue = fixture()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            self.assertTrue(root.resolve().is_relative_to(Path(__file__).resolve().parent))
            for name, obj in (("ig_learning.json", learning), ("ig_analytics.json", analytics), ("ig_state.json", {})):
                (root / name).write_text(json.dumps(obj), encoding="utf-8")
            (root / "pins.csv").write_bytes(b"Do not touch Pinterest\r\n")
            with (root / "ig_queue.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(queue[0]))
                writer.writeheader()
                writer.writerows(queue)
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            args = ["--input-dir", str(root), "--output-dir", str(root), "--start-date", START.isoformat()]
            with patch("socket.socket", side_effect=AssertionError("Network forbidden")):
                self.assertEqual(planner.main(args), 0)
            outputs = {name: (root / name).read_bytes() for name in ("ig_content_plan.json", "ig_content_plan.md")}
            self.assertEqual(planner.main(args), 0)
            for name, raw in before.items():
                self.assertEqual((root / name).read_bytes(), raw)
            for name, raw in outputs.items():
                self.assertEqual((root / name).read_bytes(), raw)
            plan = json.loads(outputs["ig_content_plan.json"])
            self.assertEqual(plan["inputs_sha256"]["ig_queue.csv"], hashlib.sha256(before["ig_queue.csv"]).hexdigest())
            (root / "ig_analytics.json").write_text("{bad", encoding="utf-8")
            with patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(planner.main(args), 2)
            self.assertEqual((root / "ig_content_plan.json").read_bytes(), outputs["ig_content_plan.json"])


if __name__ == "__main__":
    unittest.main()
