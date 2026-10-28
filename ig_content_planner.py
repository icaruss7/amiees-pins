#!/usr/bin/env python3
"""Build a deterministic, recommendation-only Instagram testing roadmap.

No network calls, API keys, publishing, or queue writes. Python standard library.
See CONTENT-PLANNER.md for thresholds, scheduling rules and limitations.
"""

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import statistics
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
LOCAL = ZoneInfo("America/New_York")
DAYS = 7
LOOKBACK_DAYS = 7
LOOKAHEAD_DAYS = 14  # after the last planning day, not just after day one
MATURE_HOURS = 72
MIN_REACH = 100
MIN_REPEATS = 3
MIN_ACTIONS = 10
STALE_DAYS = 2
COUNTS = ("reach", "views", "likes", "comments", "saved", "shares",
          "profile_visits", "follows", "total_interactions")
TEMPLATES = (
    ("detail_reveal", "Notice the small details in {piece}.", "profile_visits",
     "Product consideration", "a detail-led opening gives viewers a reason to visit the profile"),
    ("personal_style", "How would you style {piece}?", "comments",
     "Learn styling preferences", "a specific styling question invites useful comments"),
    ("gift_identity", "Know someone who would love {piece}?", "shares",
     "Relevant sharing", "a gift-recipient opening gives viewers a reason to share the design"),
    ("saveable_styling", "Try {piece} with a simple white tee.", "saved",
     "Useful styling content", "a practical styling opening gives viewers a reason to save the post"),
    ("design_story", "The shape that makes {piece} stand out.", "profile_visits",
     "Product consideration", "an opening about the visible design encourages profile visits"),
    ("detail_question", "What catches your eye first in {piece}?", "comments",
     "Learn design preferences", "an observation question starts relevant discussion about the piece"),
    ("everyday_styling", "Picture {piece} with your everyday outfit.", "profile_visits",
     "Product consideration", "an everyday styling opening helps viewers imagine wearing the piece"),
)
STOP_WORDS = set("a an and are as at by for from how in is it of on or our the this to with your you".split())


def clean(value):
    return str(value or "").replace("\u2014", ", ").replace("\u2013", "-").strip()


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        # The repository queue explicitly stores naive release_at values in UTC.
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def first_line(value):
    return next((clean(line) for line in str(value or "").splitlines() if line.strip()), "")


def piece_name(product):
    for keyword, label in (("set", "this set"), ("earring", "these earrings"),
                           ("necklace", "this necklace"), ("bracelet", "this bracelet"),
                           ("ring", "this ring"), ("pin", "this pin")):
        if keyword in product.split("-") or keyword + "s" in product.split("-"):
            return label
    return "this piece"


def tokens(text):
    return set(re.findall(r"[a-z0-9]+", clean(text).lower())) - STOP_WORDS


def similar_hook(left, right):
    a, b = clean(left).lower(), clean(right).lower()
    if not a or not b:
        return False
    x, y = tokens(a), tokens(b)
    return (a == b or SequenceMatcher(None, a, b).ratio() >= 0.8
            or (len(x & y) >= 3 and len(x & y) / len(x | y) >= 0.6))


def concept(row):
    explicit = row.get("concept") or row.get("content_angle")
    if explicit:
        return re.sub(r"[^a-z0-9]+", "_", explicit.lower()).strip("_")
    if row["post_id"].startswith("ig-nature-"):
        return "nature_fact_story"
    text = first_line(row.get("hook") or row.get("caption")).lower()
    for phrase, label in (("who would", "gift_identity"), ("know someone", "gift_identity"), ("gift", "gift_identity"),
                          ("how would you style", "personal_style"),
                          ("outfit idea", "saveable_styling"),
                          ("simple white tee", "saveable_styling"),
                          ("catches your eye", "detail_question"),
                          ("everyday outfit", "everyday_styling"),
                          ("small details", "detail_reveal"),
                          ("shape that", "design_story")):
        if phrase in text:
            return label
    return "product_showcase"


def load_inputs(directory):
    data, fingerprints = {}, {}
    for filename in ("ig_learning.json", "ig_analytics.json", "ig_queue.csv", "ig_state.json"):
        path = directory / filename
        if filename == "ig_state.json" and not path.exists():
            continue
        raw = path.read_bytes()
        fingerprints[filename] = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8-sig")
        if filename.endswith(".csv"):
            reader = csv.DictReader(io.StringIO(text, newline=""))
            required = {"post_id", "release_at", "product", "caption", "status"}
            if not required.issubset(reader.fieldnames or []):
                raise ValueError("ig_queue.csv is missing required columns")
            data[filename] = list(reader)
        else:
            # JSON NaN/Infinity are not valid inputs; metric nulls remain unknown.
            def invalid_constant(_value):
                raise ValueError(f"{filename} contains a non-finite JSON number")
            data[filename] = json.loads(text, parse_constant=invalid_constant)
            if not isinstance(data[filename], dict):
                raise ValueError(f"{filename} must contain a JSON object")
            for key in ({"ig_analytics.json": ("posts", "last_run"),
                         "ig_learning.json": ("data_notes", "benchmarks")}.get(filename, ())):
                if key in data[filename] and not isinstance(data[filename][key], dict):
                    raise ValueError(f"{filename} {key} must contain a JSON object")
    return data, fingerprints


def normalize_posts(analytics, warnings):
    source = analytics.get("posts", {})
    if not isinstance(source, dict):
        raise ValueError("ig_analytics.json posts must be an object keyed by post_id")
    posts, seen = [], set()
    as_of = timestamp((analytics.get("last_run") or {}).get("collected_at") or analytics.get("updated_at"))
    for post_id, original in sorted(source.items()):
        if not isinstance(original, dict):
            raise ValueError(f"Invalid analytics record: {post_id}")
        identity = original.get("media_id") or post_id
        if identity in seen:
            warnings.append(f"Duplicate media record excluded from evidence: {post_id}")
            continue
        seen.add(identity)
        latest = original.get("latest") or {}
        if not isinstance(latest, dict):
            raise ValueError(f"Invalid latest metrics: {post_id}")
        history = original.get("history") or []
        if not isinstance(history, list):
            raise ValueError(f"Invalid metric history: {post_id}")
        collected = max((timestamp(h.get("collected_at")) for h in history
                         if isinstance(h, dict) and timestamp(h.get("collected_at"))), default=None)
        if collected and as_of and collected > as_of:
            warnings.append(f"Future snapshot excluded from mature evidence: {post_id}")
            collected = None
        published_at = timestamp(original.get("posted_at"))
        if published_at and as_of and published_at > as_of:
            warnings.append(f"Future publication timestamp is not proof of publication: {post_id}")
            published_at = None
        posts.append({
            "post_id": clean(post_id), "product": clean(original.get("product")),
            "posted_at": published_at,
            "collected_at": collected,
            "fresh_snapshot": bool(collected and as_of and timedelta(0) <= as_of - collected <= timedelta(days=STALE_DAYS)),
            "format": {"CAROUSEL_ALBUM": "CAROUSEL", "IMAGE": "IMAGE"}.get(original.get("media_type")),
            "hook": first_line(original.get("caption")),
            "metrics": {key: number(latest.get(key)) for key in COUNTS},
            "permalink": clean(original.get("permalink")),
        })
    return posts


def mature(post):
    published, collected = post["posted_at"], post["collected_at"]
    return bool(published and collected and collected - published >= timedelta(hours=MATURE_HOURS))


def evidence(posts, primary, target_format, fresh):
    comparable = [p for p in posts if mature(p) and p["format"] == target_format]
    measured = [p for p in comparable if p["metrics"]["reach"] is not None
                and p["metrics"][primary] is not None]
    sufficient = [p for p in measured if p["metrics"]["reach"] >= MIN_REACH and p["fresh_snapshot"]]
    actions = sum(p["metrics"][primary] for p in sufficient)
    confidence = "medium" if fresh and len(sufficient) >= MIN_REPEATS and actions >= MIN_ACTIONS else "low"
    if not comparable:
        assessment = "Exploratory test; no mature same-format product comparator is available."
    elif not measured:
        assessment = "Required metrics are unavailable; collect them before evaluating this test."
    elif confidence == "low":
        observed = sum(p["metrics"][primary] for p in measured)
        assessment = ("Promising signal, needs another test." if observed > 0
                      else "No positive signal for this metric yet; needs a test, not a rejection.")
    else:
        assessment = "Repeated descriptive signal, needs a controlled follow-up; causality is unproven."
    return {
        "confidence_level": confidence, "assessment": assessment,
        "product_posts": len(posts), "mature_same_format_posts": len(comparable),
        "posts_with_required_counts": len(measured),
        "posts_with_at_least_100_reach": len(sufficient),
        "primary_actions_in_sufficient_posts": actions if measured else None,
        "basis": "Distinct published media, latest snapshot only; daily history is not extra samples.",
        "observations": [{"post_id": p["post_id"], "reach": p["metrics"]["reach"],
                          "views": p["metrics"]["views"], primary: p["metrics"][primary],
                          "posted_at": p["posted_at"].isoformat() if p["posted_at"] else None,
                          "collected_at": p["collected_at"].isoformat() if p["collected_at"] else None}
                         for p in comparable],
    }


def schedule_context(queue, posts, state, start):
    published_ids = {p["post_id"] for p in posts if p["posted_at"]}
    published_ids.update(key for key, value in state.items() if isinstance(value, dict)
                         and any(value.get(k) for k in ("media_id", "permalink", "posted_at")))
    seen, scheduled, catalog = set(), [], {}
    for row in queue:
        post_id = row.get("post_id", "").strip()
        if not post_id or post_id in seen:
            raise ValueError("ig_queue.csv must have non-empty, unique post_id values")
        seen.add(post_id)
        if row.get("status", "").strip().lower() != "queued":
            continue  # held rows never resurrect a product or a post
        product = row.get("product", "").strip()
        released = timestamp(row.get("release_at"))
        if not product or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", product) or released is None:
            raise ValueError(f"Queued row has an invalid product or release_at: {post_id}")
        images = [url.strip() for url in (row.get("images") or "").split("|") if url.strip()]
        current = catalog.setdefault(product, {"product": product, "image_count": 0})
        current["image_count"] = max(current["image_count"], len(images))
        if post_id in published_ids:
            continue
        local_date = released.astimezone(LOCAL).date()
        scheduled.append({"post_id": clean(post_id), "product": product,
                          "release_at_utc": released.astimezone(timezone.utc).isoformat(),
                          "release_at_local": released.astimezone(LOCAL).isoformat(),
                          "date": local_date.isoformat(),
                          "series": "nature" if post_id.startswith("ig-nature-") else "product",
                          "hook": first_line(row.get("hook") or row.get("caption")),
                          "concept": concept(row), "overdue": local_date < start})
    return sorted(scheduled, key=lambda r: (r["release_at_utc"], r["post_id"])), catalog


def build_plan(learning, analytics, queue, start, state=None, fingerprints=None):
    """Pure function: identical inputs and start date produce identical plans."""
    warnings = []
    posts = normalize_posts(analytics, warnings)
    scheduled, catalog = schedule_context(queue, posts, state or {}, start)
    end = start + timedelta(days=DAYS - 1)
    protected_start = start - timedelta(days=LOOKBACK_DAYS)
    protected_end = end + timedelta(days=LOOKAHEAD_DAYS)
    protected = [r for r in scheduled if protected_start.isoformat() <= r["date"] <= protected_end.isoformat()
                 or r["overdue"]]  # unresolved backlog is not free creative inventory
    blocked_products = {r["product"] for r in protected}
    recent_posts = [p for p in posts if p["posted_at"]
                    and protected_start <= p["posted_at"].astimezone(LOCAL).date() < start]
    blocked_products.update(p["product"] for p in recent_posts)
    blocked_hooks = [r["hook"] for r in protected] + [p["hook"] for p in recent_posts]
    blocked_concepts = {r["concept"] for r in protected}
    collected = timestamp((analytics.get("last_run") or {}).get("collected_at") or analytics.get("updated_at"))
    anchor = datetime.combine(start, time.min, tzinfo=LOCAL)
    fresh = collected is not None and timedelta(0) <= anchor - collected <= timedelta(days=STALE_DAYS)
    if not fresh:
        warnings.append("Analytics are stale, future-dated or undated relative to the plan start; confidence is low.")
    learned_at = (learning.get("data_notes") or {}).get("collected_at")
    if learned_at and timestamp(learned_at) != collected:
        warnings.append("Learning and analytics collection times differ; analytics counts take precedence.")
    if number(learning.get("sample_size")) != len(posts):
        warnings.append("Learning sample size differs from distinct analytics records; no ranking is inherited.")
    if (analytics.get("last_run") or {}).get("posts_skipped"):
        warnings.append("The analytics collector skipped posts; absent records are not zero-performance posts.")
    if not state:
        warnings.append("No publication-state records available; uncollected queued rows are conservatively reserved.")
    reaches = [p["metrics"]["reach"] for p in posts if p["metrics"]["reach"] is not None]
    mature_posts = [p for p in posts if mature(p)]
    products = {key: [p for p in posts if p["product"] == key] for key in catalog}
    candidates = [item for key, item in catalog.items() if key not in blocked_products and item["image_count"] > 0]
    # Retest measured products first, without ranking tiny percentages or likes.
    # Stable weekly rotation breaks ties, so an unchanged catalog gets exploration.
    candidates.sort(key=lambda c: (
        not any(mature(p) for p in products[c["product"]]),
        hashlib.sha256(f"{start.isoformat()}:{c['product']}".encode()).hexdigest(), c["product"]))
    recommendations, used_products, used_concepts, used_hooks = [], set(), set(), []
    for day in range(DAYS):
        selected = None
        for offset in range(len(TEMPLATES)):
            angle, hook_template, metric, objective, idea = TEMPLATES[(day + offset) % len(TEMPLATES)]
            if angle in blocked_concepts or angle in used_concepts:
                continue
            for candidate in candidates:
                product = candidate["product"]
                if product in used_products:
                    continue
                name = product.replace("-", " ").title()
                hook = hook_template.format(name=name, piece=piece_name(product))
                if any(similar_hook(hook, h) for h in blocked_hooks + used_hooks):
                    continue
                baseline = sorted((p for p in products[product] if mature(p) and p["format"]
                                   and candidate["image_count"] >= (2 if p["format"] == "CAROUSEL" else 1)),
                                  key=lambda p: (p["posted_at"], p["post_id"]), reverse=True)
                control = baseline[0] if baseline else None
                if control and similar_hook(hook, control["hook"]):
                    continue
                target_format = control["format"] if control else (
                    "CAROUSEL" if candidate["image_count"] >= 2 else "IMAGE")
                available = any(p["format"] == target_format and p["metrics"][metric] is not None
                                and p["metrics"]["reach"] is not None for p in posts)
                primary = metric if available else "reach"
                if not available:
                    objective = "Establish a discovery baseline"
                support = evidence(products[product], primary, target_format, fresh)
                if primary == "reach":
                    support["confidence_level"] = "low"  # fallback reach cannot support a behavioral conclusion
                controlled = control is not None
                hypothesis = (f"With the other elements fixed, test whether {idea}."
                              if controlled else f"Test whether {idea}; this product has no mature same-format comparator.")
                if primary == "reach":
                    hypothesis += f" {metric} is not yet measured for this format; evaluate reach only until insights are available."
                selected = {
                    "id": f"test-{start.isoformat()}-{day + 1:02d}",
                    "review_date": (start + timedelta(days=day)).isoformat(),
                    "status": "recommendation_only_requires_review",
                    "product": product, "product_name": name,
                    "content_angle": angle, "hook": hook, "format": target_format,
                    "objective": objective, "hypothesis": hypothesis,
                    "primary_success_metric": {
                        "name": f"{primary}_per_reached_account" if primary != "reach" else "reach",
                        "raw_count": primary, "denominator": "reach" if primary != "reach" else None,
                        "definition": f"{primary} / reach, accompanied by both raw counts" if primary != "reach"
                                      else "Accounts reached, not views or impressions",
                        "evaluation_age_hours": MATURE_HOURS,
                        "missing_data": "Unknown; do not replace missing counts with zero or substitute views for reach.",
                    },
                    "confidence_level": support["confidence_level"], "evidence": support,
                    "test_type": "controlled_follow_up" if controlled else "exploratory_test",
                    "control": {"reference_post_id": control["post_id"],
                                "reference_hook": control["hook"],
                                "reference_permalink": control["permalink"],
                                "change_only": "opening_caption_line",
                                "hold_constant": ["product and variant", "exact images and their order", "format",
                                                  "caption after first line", "CTA and offer", "posting-time window"],
                                "comparison_limit": "Historical lifetime counts are context only. A matched-age control is required for evaluation."
                                } if control else None,
                    "production_brief": ("Reuse the reference post's exact approved media and caption after the first line. "
                                         "Change only the opening line; verify assets before approval." if controlled else
                                         "Use approved, accurate product media. Plan one clear styling/detail treatment; do not invent product claims."),
                    "placement": "Editorial review backlog. Do not add to or replace the daily Nature Series automatically.",
                    "schedule_check": {"protected_product_collision": False, "protected_concept_collision": False,
                                       "protected_hook_collision": False},
                    "evaluation_rule": ("Record counts at 72 hours for control and variant. Review direction only after three comparable "
                                        "posts per condition, each reaching at least 100 accounts, and at least 10 primary actions "
                                        "per condition for action metrics. These are planning guardrails, not statistical proof. "
                                        "If reach stays small, keep confidence low and revisit next week without increasing posting volume."),
                }
                break
            if selected:
                used_concepts.add(angle)
                break
        if selected:
            recommendations.append(selected)
            used_products.add(selected["product"])
            used_hooks.append(selected["hook"])
    days = [{"date": (start + timedelta(days=i)).isoformat(),
             "scheduled_posts": [r for r in scheduled if r["date"] == (start + timedelta(days=i)).isoformat()],
             "recommendation_ids": [r["id"] for r in recommendations if r["review_date"] == (start + timedelta(days=i)).isoformat()],
             "action": "Review proposed test; preserve scheduled posts." if any(
                 r["review_date"] == (start + timedelta(days=i)).isoformat() for r in recommendations)
             else "Observe scheduled content; no safe non-overlapping test candidate is available."}
            for i in range(DAYS)]
    if len(recommendations) < DAYS:
        warnings.append("Fewer than seven safe tests were found. Empty review days do not authorize duplicate or invented content.")
    return {
        "schema_version": 1, "planner_version": 1, "mode": "deterministic_rules_only",
        "timezone": "America/New_York", "start_date": start.isoformat(), "end_date": end.isoformat(),
        "inputs_sha256": fingerprints or {},
        "safety": {"recommendation_only": True, "auto_publish": False, "queue_writes": False,
                   "new_paid_service_required": False, "publishing_frequency_change": False},
        "data_summary": {
            "analytics_collected_at": collected.isoformat() if collected else None,
            "analytics_fresh": fresh, "distinct_posts": len(posts), "mature_posts": len(mature_posts),
            "median_reach": statistics.median(reaches) if reaches else None,
            "sum_post_reach_not_unique_audience": sum(reaches) if reaches else None,
            "metric_availability_posts": {key: sum(p["metrics"][key] is not None for p in posts) for key in COUNTS},
            "learning_sample_size": number(learning.get("sample_size")),
            "learning_median_reach_or_views": number((learning.get("benchmarks") or {}).get("median_reach_or_views")),
            "upstream_ranking_policy": "Read learning context but do not inherit rankings, percentage claims or winner labels.",
            "sample_warning": "High percentages from a few reached accounts do not establish repeatable performance. No automatic winner decisions.",
        },
        "deduplication": {"protected_start": protected_start.isoformat(), "protected_end": protected_end.isoformat(),
                          "lookback_days": LOOKBACK_DAYS, "lookahead_days_after_plan_end": LOOKAHEAD_DAYS,
                          "protected_products": sorted(blocked_products), "protected_concepts": sorted(blocked_concepts),
                          "protected_scheduled_posts": protected,
                          "method": "Product handles, explicit/keyword concept categories, normalized and fuzzy opening hooks; not semantic AI.",
                          "collision_exceptions": "None. Even controlled follow-ups avoid the protected schedule."},
        "days": days, "recommendations": recommendations, "warnings": warnings,
        "review_requirements": [
            "Choose or defer proposed tests without displacing Nature Series or increasing frequency by default.",
            "Confirm current product availability, exact media, rights, claims and destination before any later queue edit.",
            "Pre-register a matched-age control and the single variable before approving a follow-up test.",
            "Do not infer sales, unique audience, optimal posting time or causal format effects from these snapshots.",
        ],
    }


def render_markdown(plan):
    data = plan["data_summary"]
    lines = ["# Amiees Instagram content testing plan", "",
             f"Review week: {plan['start_date']} to {plan['end_date']} ({plan['timezone']}).", "",
             "Recommendations only. These dates are editorial review days, not new posting slots. "
             "Keep the Nature Series and both posting queues unchanged. Seven ideas do not authorize seven extra posts.", "",
             "## Evidence and limits", "",
             f"- Analytics collected: {data['analytics_collected_at'] or 'unknown'}.",
             f"- Distinct tracked posts: {data['distinct_posts']}; mature snapshots: {data['mature_posts']}.",
             f"- Median reached accounts per post: {data['median_reach'] if data['median_reach'] is not None else 'unknown'}.",
             f"- {data['sample_warning']}",
             "- Learning context is read, but its ranking language is not inherited. Repeated daily snapshots are not additional post samples.",
             f"- Protect queued products, concepts and hooks from {plan['deduplication']['protected_start']} "
             f"through {plan['deduplication']['protected_end']}, plus unresolved overdue queue entries.", "",
             "## Seven-day roadmap", ""]
    by_id = {r["id"]: r for r in plan["recommendations"]}
    for day in plan["days"]:
        lines += [f"### {day['date']}", "", f"- Action: {day['action']}"]
        if day["scheduled_posts"]:
            for row in day["scheduled_posts"]:
                lines.append(f"- Preserve scheduled {row['series']} post: {row['product']} "
                             f"at {row['release_at_local']} ({row['post_id']}).")
        else:
            lines.append("- No unposted queue row is scheduled for this day; this is not permission to fill it.")
        for key in day["recommendation_ids"]:
            row = by_id[key]
            lines += [f"- Product: **{row['product_name']}** (`{row['product']}`).",
                      f"- Content angle: {row['content_angle']}.", f"- Hook: {row['hook']}",
                      f"- Format: {row['format']}.", f"- Objective: {row['objective']}.",
                      f"- Hypothesis: {row['hypothesis']}",
                      f"- Primary success metric: {row['primary_success_metric']['definition']} at 72 hours.",
                      f"- Confidence: **{row['confidence_level']}**. {row['evidence']['assessment']}",
                      f"- Test type: {row['test_type']}."]
            if row["control"]:
                ctrl = row["control"]
                lines += [f"- Historical reference: {ctrl['reference_post_id']}. Change only the opening caption line; "
                          "keep the product/variant, exact images/order, format, remaining caption, offer/CTA and posting-time window fixed.",
                          f"- Reference opening: {ctrl['reference_hook']}", f"- Comparison limit: {ctrl['comparison_limit']}"]
            observations = row["evidence"]["observations"]
            if observations:
                metric = row["primary_success_metric"]["raw_count"]
                for obs in observations:
                    lines.append(f"- Evidence ({obs['post_id']}): reach {obs['reach'] if obs['reach'] is not None else 'unknown'}, "
                                 f"{metric} {obs.get(metric) if obs.get(metric) is not None else 'unknown'}; latest snapshot, not a matched-age result.")
            lines += [f"- Production brief: {row['production_brief']}", f"- Placement: {row['placement']}",
                      f"- Evaluation: {row['evaluation_rule']}"]
        lines.append("")
    lines += ["## Review before activation", ""]
    lines += [f"- {item}" for item in plan["review_requirements"]]
    if plan["warnings"]:
        lines += ["", "## Input warnings", ""] + [f"- {w}" for w in plan["warnings"]]
    lines += ["", "## Input fingerprints", ""]
    lines += [f"- `{name}`: `{digest}`" for name, digest in sorted(plan["inputs_sha256"].items())]
    return clean("\n".join(lines)) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=HERE)
    parser.add_argument("--output-dir", type=Path, default=HERE)
    parser.add_argument("--start-date", type=date.fromisoformat,
                        default=None, help="First review day, YYYY-MM-DD; default: tomorrow in New York")
    args = parser.parse_args(argv)
    start = args.start_date or datetime.now(LOCAL).date() + timedelta(days=1)
    try:
        data, hashes = load_inputs(args.input_dir)
        plan = build_plan(data["ig_learning.json"], data["ig_analytics.json"], data["ig_queue.csv"],
                          start, data.get("ig_state.json"), hashes)
        output = {"ig_content_plan.json": json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
                  "ig_content_plan.md": render_markdown(plan)}
        args.output_dir.mkdir(parents=True, exist_ok=True)
        # Validate every input before touching outputs. Replace individual outputs atomically.
        for filename, text in output.items():
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=args.output_dir, delete=False) as fh:
                temporary = Path(fh.name)
                fh.write(text)
            try:
                os.replace(temporary, args.output_dir / filename)
            finally:
                temporary.unlink(missing_ok=True)
        print(f"Wrote a seven-day review plan with {len(plan['recommendations'])} proposed tests. No queues changed.")
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"Content planner failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
