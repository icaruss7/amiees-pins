#!/usr/bin/env python3
"""
Collect durable Instagram post analytics for Amiees.

Reads published posts from ig_state.json, resolves missing media IDs from the
connected Instagram account, fetches metadata and supported media insights,
and stores at most one snapshot per UTC day in ig_analytics.json.

Meta changes metric availability across media types and API versions. This
collector therefore tries a broad metric set in batches, recursively splits
rejected batches, and records unsupported metrics instead of failing the run.

Needs IG_TOKEN. IG_USER_ID is optional.
Standard library only.
"""

import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "ig_state.json")
QUEUE = os.path.join(HERE, "ig_queue.csv")
OUT = os.path.join(HERE, "ig_analytics.json")

API = "https://graph.facebook.com/" + os.environ.get("GRAPH_VERSION", "v26.0")
TOKEN = os.environ.get("IG_TOKEN", "").strip()
IG_USER_ID = os.environ.get("IG_USER_ID", "").strip()
MAX_HISTORY = int(os.environ.get("IG_ANALYTICS_HISTORY_DAYS", "90"))

CANDIDATE_METRICS = [
    "reach",
    "views",
    "likes",
    "comments",
    "shares",
    "saved",
    "total_interactions",
    "reposts",
    "follows",
    "profile_visits",
    "profile_activity",
    "ig_reels_avg_watch_time",
    "ig_reels_video_view_total_time",
    "reels_skip_rate",
]


class GraphError(RuntimeError):
    def __init__(self, message, status=None, code=None, subcode=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.subcode = subcode


def get(path, **params):
    params["access_token"] = TOKEN
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{API}/{path}?{query}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        code = subcode = None
        message = body
        try:
            err = json.loads(body).get("error", {})
            code = err.get("code")
            subcode = err.get("error_subcode")
            message = err.get("message") or body
        except (ValueError, TypeError, AttributeError):
            pass
        raise GraphError(
            f"GET {path} failed ({exc.code}): {message}",
            status=exc.code,
            code=code,
            subcode=subcode,
        ) from None


def username_of(ig_id):
    try:
        return get(ig_id, fields="username").get("username", "")
    except GraphError:
        return ""


def find_ig_user():
    if IG_USER_ID:
        return IG_USER_ID, username_of(IG_USER_ID) or "(from IG_USER_ID)"

    notes = []
    try:
        info = get("debug_token", input_token=TOKEN).get("data", {})
        for grant in info.get("granular_scopes", []):
            if grant.get("scope") in (
                "instagram_basic",
                "instagram_manage_insights",
                "instagram_content_publish",
            ):
                for ig_id in grant.get("target_ids", []):
                    name = username_of(ig_id)
                    if name:
                        return ig_id, name
    except GraphError as exc:
        notes.append(str(exc))

    try:
        pages = get(
            "me/accounts",
            fields="name,instagram_business_account{id,username}",
        ).get("data", [])
        for page in pages:
            account = page.get("instagram_business_account")
            if account:
                return account["id"], account.get("username", "")
    except GraphError as exc:
        notes.append(str(exc))

    raise RuntimeError(
        "Could not find the Instagram account for analytics."
        + (("\n  " + "\n  ".join(notes)) if notes else "")
    )


def load_queue():
    with open(QUEUE, encoding="utf-8-sig", newline="") as fh:
        return {row["post_id"]: row for row in csv.DictReader(fh)}


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.write("\n")


def fetch_account_profile(ig_user):
    fields = "id,username,followers_count,follows_count,media_count"
    try:
        return get(ig_user, fields=fields)
    except GraphError as exc:
        print(f"WARNING: account profile metrics unavailable: {exc}")
        return {"id": ig_user}


def fetch_recent_media(ig_user):
    fields = "id,permalink,media_type,media_product_type,timestamp,like_count,comments_count"
    try:
        rows = get(ig_user + "/media", fields=fields, limit=100).get("data", [])
    except GraphError as exc:
        print(f"WARNING: could not list recent media: {exc}")
        return {}, {}
    by_link = {row.get("permalink"): row for row in rows if row.get("permalink")}
    by_id = {row.get("id"): row for row in rows if row.get("id")}
    return by_link, by_id


def fetch_media_metadata(media_id, cached=None):
    if cached:
        meta = dict(cached)
    else:
        fields = "id,permalink,media_type,media_product_type,timestamp,like_count,comments_count"
        try:
            meta = get(media_id, fields=fields)
        except GraphError as exc:
            print(f"WARNING {media_id}: metadata unavailable: {exc}")
            meta = {"id": media_id}

    # Facebook Login exposes shares_count on some current media/API versions.
    try:
        extra = get(media_id, fields="shares_count")
        if "shares_count" in extra:
            meta["shares_count"] = extra["shares_count"]
    except GraphError:
        pass
    return meta


def permission_error(exc):
    text = str(exc).lower()
    return (
        "permission" in text
        or "instagram_manage_insights" in text
        or ("requires" in text and "insight" in text)
    )


def normalize_value(item):
    if "total_value" in item:
        value = item["total_value"]
        if isinstance(value, dict) and "value" in value:
            return value["value"]
        return value
    values = item.get("values") or []
    if values:
        return values[-1].get("value")
    return None


def parse_insights(payload):
    out = {}
    for item in payload.get("data", []):
        name = item.get("name")
        if name:
            out[name] = normalize_value(item)
    return out


def fetch_metric_batch(media_id, metrics, unsupported, permission_notes):
    if not metrics:
        return {}
    try:
        return parse_insights(get(media_id + "/insights", metric=",".join(metrics)))
    except GraphError as exc:
        if permission_error(exc):
            permission_notes.add(str(exc))
            for metric in metrics:
                unsupported.setdefault(metric, "permission unavailable")
            return {}
        if len(metrics) == 1:
            unsupported[metrics[0]] = str(exc)
            return {}
        mid = len(metrics) // 2
        left = fetch_metric_batch(
            media_id, metrics[:mid], unsupported, permission_notes
        )
        right = fetch_metric_batch(
            media_id, metrics[mid:], unsupported, permission_notes
        )
        left.update(right)
        return left


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def enrich_metrics(metrics, metadata):
    # Keep useful base counts even before Insights permission is added.
    if "likes" not in metrics and numeric(metadata.get("like_count")):
        metrics["likes"] = metadata["like_count"]
    if "comments" not in metrics and numeric(metadata.get("comments_count")):
        metrics["comments"] = metadata["comments_count"]
    if "shares" not in metrics and numeric(metadata.get("shares_count")):
        metrics["shares"] = metadata["shares_count"]

    reach = metrics.get("reach")
    views = metrics.get("views")
    denom = (
        reach
        if numeric(reach) and reach > 0
        else views
        if numeric(views) and views > 0
        else None
    )
    if denom:
        for source, target in (
            ("likes", "like_rate"),
            ("comments", "comment_rate"),
            ("shares", "share_rate"),
            ("saved", "save_rate"),
            ("total_interactions", "interaction_rate"),
            ("follows", "follow_rate"),
            ("profile_visits", "profile_visit_rate"),
        ):
            value = metrics.get(source)
            if numeric(value):
                metrics[target] = round(value / denom, 6)
    return metrics


def upsert_daily(history, snapshot):
    day = snapshot["collected_at"][:10]
    history = [h for h in history if h.get("collected_at", "")[:10] != day]
    history.append(snapshot)
    history.sort(key=lambda h: h.get("collected_at", ""))
    return history[-MAX_HISTORY:]


def main():
    if not TOKEN:
        print("IG_TOKEN is not set; analytics collection skipped.")
        return 0

    state = load_json(STATE, {})
    queue = load_queue()
    analytics = load_json(
        OUT,
        {
            "schema_version": 1,
            "updated_at": None,
            "account_history": [],
            "posts": {},
            "last_run": {},
        },
    )

    ig_user, username = find_ig_user()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"Collecting Instagram analytics for @{username} ({ig_user})")

    account = fetch_account_profile(ig_user)
    account_snapshot = {
        "collected_at": now,
        **{
            key: account[key]
            for key in (
                "id",
                "username",
                "followers_count",
                "follows_count",
                "media_count",
            )
            if key in account
        },
    }
    analytics["account_history"] = upsert_daily(
        analytics.get("account_history", []), account_snapshot
    )

    by_link, by_id = fetch_recent_media(ig_user)
    permission_notes = set()
    collected = 0
    skipped = 0

    for post_id, published in sorted(state.items()):
        if not published.get("posted_at") and not published.get("permalink"):
            continue

        media_id = published.get("media_id")
        cached = by_id.get(media_id) if media_id else None
        if not media_id and published.get("permalink"):
            cached = by_link.get(published["permalink"])
            if cached:
                media_id = cached.get("id")
        if not media_id:
            print(f"WARNING {post_id}: could not resolve media_id; skipped.")
            skipped += 1
            continue

        metadata = fetch_media_metadata(media_id, cached)
        unsupported = {}
        metrics = fetch_metric_batch(
            media_id,
            CANDIDATE_METRICS,
            unsupported,
            permission_notes,
        )
        metrics = enrich_metrics(metrics, metadata)
        # If a count was recovered from the ordinary media metadata endpoint,
        # it is usable even though the Insights endpoint itself rejected it.
        for recovered in list(metrics):
            unsupported.pop(recovered, None)

        row = queue.get(post_id, {})
        post = analytics.setdefault("posts", {}).setdefault(post_id, {})
        post.update(
            {
                "post_id": post_id,
                "media_id": media_id,
                "permalink": published.get("permalink") or metadata.get("permalink"),
                "posted_at": published.get("posted_at") or metadata.get("timestamp"),
                "product": row.get("product", ""),
                "caption": row.get("caption", ""),
                "series": "nature" if post_id.startswith("ig-nature-") else "product",
                "media_type": metadata.get("media_type"),
                "media_product_type": metadata.get("media_product_type"),
                "latest": metrics,
                "unsupported_metrics": unsupported,
            }
        )
        post["history"] = upsert_daily(
            post.get("history", []),
            {"collected_at": now, "metrics": metrics},
        )
        collected += 1

    analytics["updated_at"] = now
    analytics["last_run"] = {
        "collected_at": now,
        "posts_collected": collected,
        "posts_skipped": skipped,
        "insights_permission_notes": sorted(permission_notes),
    }
    save_json(OUT, analytics)

    print(f"Collected {collected} published post(s); skipped {skipped}.")
    if permission_notes:
        print(
            "NOTE: Meta Insights permission appears unavailable. Base like/comment "
            "counts were still stored. Add instagram_manage_insights to the token "
            "to unlock reach, views, saves, shares and other insight metrics."
        )
        for note in sorted(permission_notes):
            print("  " + note)
    else:
        available = sorted(
            {
                key
                for post in analytics.get("posts", {}).values()
                for key in post.get("latest", {})
                if not key.endswith("_rate")
            }
        )
        print(
            "Insight metrics currently exposed: "
            + (", ".join(available) or "none")
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
