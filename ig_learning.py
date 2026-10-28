#!/usr/bin/env python3
"""
Build machine-readable and human-readable Amiees Instagram learning summaries
from ig_analytics.json.

Outputs:
  - ig_learning.json
  - ig_weekly_report.md

Standard library only.
"""

import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ANALYTICS = os.path.join(HERE, "ig_analytics.json")
LEARNING = os.path.join(HERE, "ig_learning.json")
REPORT = os.path.join(HERE, "ig_weekly_report.md")


def load(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.write("\n")


def num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def metric(post, name):
    value = post.get("latest", {}).get(name)
    return float(value) if num(value) else None


def median(values):
    values = [v for v in values if num(v)]
    return statistics.median(values) if values else None


def mean(values):
    values = [v for v in values if num(v)]
    return statistics.fmean(values) if values else None


def fmt_count(value):
    if value is None:
        return "n/a"
    return f"{value:,.0f}"


def fmt_rate(value):
    return "n/a" if value is None else f"{value * 100:.2f}%"


def safe_iso(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def post_score(post, med_reach):
    share_rate = metric(post, "share_rate") or 0.0
    save_rate = metric(post, "save_rate") or 0.0
    interact_rate = metric(post, "interaction_rate") or 0.0
    reach = metric(post, "reach") or metric(post, "views") or 0.0
    if med_reach and med_reach > 0:
        reach_component = min(reach / med_reach, 2.0) / 2.0
    else:
        reach_component = 0.0
    return (
        0.40 * share_rate
        + 0.25 * save_rate
        + 0.25 * interact_rate
        + 0.10 * reach_component
    )


def group_summary(posts, key_func):
    groups = defaultdict(list)
    for post in posts:
        key = key_func(post)
        if key:
            groups[key].append(post)

    out = {}
    for key, members in groups.items():
        out[key] = {
            "n": len(members),
            "avg_reach": mean([metric(p, "reach") for p in members]),
            "avg_views": mean([metric(p, "views") for p in members]),
            "avg_share_rate": mean([metric(p, "share_rate") for p in members]),
            "avg_save_rate": mean([metric(p, "save_rate") for p in members]),
            "avg_interaction_rate": mean(
                [metric(p, "interaction_rate") for p in members]
            ),
        }
    return out


def top_group(groups, metric_name, min_n=2):
    eligible = [
        (name, data)
        for name, data in groups.items()
        if data.get("n", 0) >= min_n and num(data.get(metric_name))
    ]
    return max(eligible, key=lambda item: item[1][metric_name]) if eligible else None


def main():
    data = load(ANALYTICS, {})
    posts = list(data.get("posts", {}).values())
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    reaches = [
        metric(p, "reach") or metric(p, "views")
        for p in posts
        if metric(p, "reach") is not None or metric(p, "views") is not None
    ]
    med_reach = median(reaches)

    for post in posts:
        post["_score"] = post_score(post, med_reach)

    ranked = sorted(posts, key=lambda p: p["_score"], reverse=True)

    series = group_summary(posts, lambda p: p.get("series"))
    formats = group_summary(
        posts,
        lambda p: p.get("media_product_type") or p.get("media_type"),
    )
    weekdays = group_summary(
        posts,
        lambda p: safe_iso(p.get("posted_at")).strftime("%A")
        if safe_iso(p.get("posted_at"))
        else None,
    )
    hours = group_summary(
        posts,
        lambda p: safe_iso(p.get("posted_at")).strftime("%H:00 UTC")
        if safe_iso(p.get("posted_at"))
        else None,
    )

    recommendations = []
    if ranked:
        winners = [p.get("product") or p.get("post_id") for p in ranked[:3]]
        recommendations.append(
            "Create follow-up variants around the strongest current winners: "
            + ", ".join(winners)
            + "."
        )

    nature = series.get("nature")
    product = series.get("product")
    if nature and product and nature["n"] >= 2 and product["n"] >= 2:
        ns = nature.get("avg_share_rate")
        ps = product.get("avg_share_rate")
        if num(ns) and num(ps) and ps > 0:
            if ns >= ps * 1.20:
                recommendations.append(
                    "Nature-series posts are generating at least 20% more shares per reach "
                    "than the older product series; keep leaning into nature/story-led hooks."
                )
            elif ps >= ns * 1.20:
                recommendations.append(
                    "Straight product posts are currently generating at least 20% more "
                    "shares per reach than the nature series; test more direct product hooks."
                )

    best_format = top_group(formats, "avg_interaction_rate")
    if best_format:
        recommendations.append(
            f"{best_format[0]} is the strongest format with enough samples by "
            f"interaction rate ({fmt_rate(best_format[1]['avg_interaction_rate'])}); "
            "give it a larger share of the next test batch."
        )

    best_day = top_group(weekdays, "avg_share_rate")
    if best_day:
        recommendations.append(
            f"{best_day[0]} currently has the strongest share rate among weekdays "
            f"with at least two posts ({fmt_rate(best_day[1]['avg_share_rate'])}). "
            "Treat this as provisional until the sample grows."
        )

    overall_share = mean([metric(p, "share_rate") for p in posts])
    if num(overall_share) and overall_share < 0.005:
        recommendations.append(
            "Share rate is below 0.5%; test stronger send-worthy hooks such as "
            "'send this to the friend who would wear #2' or identity/gift framing."
        )

    if not recommendations:
        recommendations.append(
            "Keep collecting data. There is not yet enough comparable performance "
            "data to make a strong allocation change."
        )

    account_history = data.get("account_history", [])
    latest_account = account_history[-1] if account_history else {}
    follower_delta = None
    if len(account_history) >= 2:
        first = account_history[0].get("followers_count")
        last = account_history[-1].get("followers_count")
        if num(first) and num(last):
            follower_delta = last - first

    learning = {
        "generated_at": now,
        "sample_size": len(posts),
        "account": {
            "followers": latest_account.get("followers_count"),
            "follower_delta_since_tracking_started": follower_delta,
        },
        "benchmarks": {
            "median_reach_or_views": med_reach,
            "avg_share_rate": overall_share,
            "avg_save_rate": mean([metric(p, "save_rate") for p in posts]),
            "avg_interaction_rate": mean(
                [metric(p, "interaction_rate") for p in posts]
            ),
        },
        "top_posts": [
            {
                "post_id": p.get("post_id"),
                "product": p.get("product"),
                "permalink": p.get("permalink"),
                "score": round(p["_score"], 6),
                "reach": metric(p, "reach"),
                "views": metric(p, "views"),
                "share_rate": metric(p, "share_rate"),
                "save_rate": metric(p, "save_rate"),
                "interaction_rate": metric(p, "interaction_rate"),
            }
            for p in ranked[:5]
        ],
        "groups": {
            "series": series,
            "format": formats,
            "weekday": weekdays,
            "posting_hour_utc": hours,
        },
        "recommendations": recommendations,
        "data_notes": data.get("last_run", {}),
    }
    save_json(LEARNING, learning)

    lines = [
        "# Amiees Instagram Weekly Learning Report",
        "",
        f"_Generated {now}_",
        "",
        "## Snapshot",
        "",
        f"- Published posts tracked: **{len(posts)}**",
        f"- Followers: **{fmt_count(latest_account.get('followers_count'))}**",
        f"- Follower change since tracking began: **{('n/a' if follower_delta is None else f'{follower_delta:+.0f}')}**",
        f"- Median reach/views per tracked post: **{fmt_count(med_reach)}**",
        f"- Average share rate: **{fmt_rate(overall_share)}**",
        f"- Average save rate: **{fmt_rate(learning['benchmarks']['avg_save_rate'])}**",
        f"- Average interaction rate: **{fmt_rate(learning['benchmarks']['avg_interaction_rate'])}**",
        "",
        "## Strongest posts",
        "",
    ]

    if ranked:
        for i, post in enumerate(ranked[:5], 1):
            lines.append(
                f"{i}. **{post.get('product') or post.get('post_id')}** — "
                f"reach {fmt_count(metric(post, 'reach') or metric(post, 'views'))}, "
                f"shares/reach {fmt_rate(metric(post, 'share_rate'))}, "
                f"saves/reach {fmt_rate(metric(post, 'save_rate'))}, "
                f"interactions/reach {fmt_rate(metric(post, 'interaction_rate'))}"
            )
    else:
        lines.append("No post analytics collected yet.")

    lines += ["", "## Current patterns", ""]
    for label, groups in (
        ("Series", series),
        ("Format", formats),
        ("Weekday", weekdays),
    ):
        lines.append(f"### {label}")
        if not groups:
            lines.append("No comparable data yet.")
        else:
            for name, group in sorted(groups.items()):
                lines.append(
                    f"- **{name}** (n={group['n']}): "
                    f"avg reach {fmt_count(group.get('avg_reach') or group.get('avg_views'))}, "
                    f"share rate {fmt_rate(group.get('avg_share_rate'))}, "
                    f"save rate {fmt_rate(group.get('avg_save_rate'))}, "
                    f"interaction rate {fmt_rate(group.get('avg_interaction_rate'))}"
                )
        lines.append("")

    lines += ["## Recommended next tests", ""]
    for item in recommendations:
        lines.append(f"- {item}")

    permission_notes = data.get("last_run", {}).get("insights_permission_notes") or []
    if permission_notes:
        lines += [
            "",
            "## Setup note",
            "",
            "- Meta's Insights permission was not available on the current token, so "
            "the report is using only base media counts where possible. Add "
            "instagram_manage_insights and refresh the token to unlock the full learning loop.",
        ]

    with open(REPORT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")

    print(
        f"Wrote {os.path.basename(LEARNING)} and "
        f"{os.path.basename(REPORT)} for {len(posts)} posts."
    )


if __name__ == "__main__":
    main()
