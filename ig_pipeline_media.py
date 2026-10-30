"""Resolve approved pipeline media without changing the existing CSV schema."""

import hashlib
import json
import re


def audio_configuration(value):
    if not isinstance(value, dict) or set(value) != {"audio_id", "audio_volume", "video_volume"}:
        raise ValueError("Native audio needs an exact ID and both volume levels")
    if not isinstance(value["audio_id"], str) or not re.fullmatch(r"[0-9]+", value["audio_id"]):
        raise ValueError("Native audio ID must be numeric")
    if any(type(value[k]) is not int or not 1 <= value[k] <= 100 for k in ("audio_volume", "video_volume")):
        raise ValueError("Native audio volume levels must be integers from 1 to 100")
    return dict(value)


def native_audio(record):
    value = record.get("instagram_audio")
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"configuration", "title", "artist", "catalog_collected_at", "rights_review"}:
        raise ValueError("Native audio needs its configuration, track metadata and rights review")
    if record.get("platform") != "instagram" or record.get("format") != "REELS":
        raise ValueError("Native Instagram audio is for Instagram Reels only")
    config = audio_configuration(value.get("configuration"))
    review = value.get("rights_review")
    if not isinstance(review, dict) or set(review) != {"commercial_use_verified", "by", "at", "source_url"}:
        raise ValueError("Native audio needs a commercial rights review")
    if (review.get("commercial_use_verified") is not True or not review.get("by")
            or not review.get("at") or not str(review.get("source_url", "")).startswith("https://")
            or not value.get("title") or not value.get("artist") or not value.get("catalog_collected_at")):
        raise ValueError("Review commercial music rights and retain catalog evidence before approval")
    return config


def fingerprint(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def reel_media(record):
    """Bind the reviewed hosted rendition to this exact uploaded revision."""
    review = record.get("hosted_video_review") or {}
    files = {f["path"]: f for f in record.get("shopify_files", [])}
    roles = {a["role"]: files.get(a["path"], {}) for a in record["final_assets"]}
    video, cover = roles.get("video", {}), roles.get("cover", {})
    if (review.get("content_sha256") != record["content_sha256"]
            or review.get("upload_receipt_sha256") != fingerprint(record["upload_receipt"])
            or review.get("cdn_url") != video.get("cdn_url")
            or not review.get("by") or not review.get("at")
            or not re.fullmatch(r"[a-f0-9]{64}", review.get("sha256", ""))
            or not video.get("cdn_url") or not cover.get("cdn_url")):
        raise ValueError("Review the actual Shopify-hosted video rendition before scheduling")
    result = {"media_type": "REELS", "video_url": video["cdn_url"],
              "video_sha256": review["sha256"], "cover_url": cover["cdn_url"]}
    audio = native_audio(record)
    if audio:
        result["audio_configuration"] = audio
    if record.get("is_ai_generated") is True:
        result["is_ai_generated"] = True
    if record.get("schedule_mode") == "additional_reel":
        result["schedule_mode"] = "additional_reel"
    return result


def resolve_row(row, registry):
    """Legacy rows pass through; experiment rows must match both approvals."""
    identity = row.get("post_id", "")
    if not identity.startswith("ig-exp-"):
        return dict(row)
    matches = [r for r in registry.get("items", {}).values()
               if r.get("queue_id") == identity and r.get("platform") == "instagram"]
    if len(matches) != 1:
        raise ValueError("Pipeline queue row needs exactly one experiment record")
    record = matches[0]
    draft, creative, schedule = (record.get(k) or {} for k in
                                 ("queue_draft", "approval", "schedule_approval"))
    if (record.get("publishing_status") not in ("scheduled", "published", "measured")
            or identity != "ig-exp-" + record["experiment_id"]
            or record["product"] != row.get("product")
            or not creative.get("by") or not schedule.get("by")
            or creative.get("content_sha256") != record["content_sha256"]
            or draft.get("content_sha256") != record["content_sha256"]
            or draft.get("upload_receipt_sha256") != fingerprint(record["upload_receipt"])
            or draft.get("row") != row
            or schedule.get("draft_sha256") != fingerprint(draft)):
        raise ValueError("Pipeline queue row differs from the approved content or schedule")
    if record["format"] == "REELS":
        media = reel_media(record)
        if draft.get("media") != media:
            raise ValueError("Hosted Reel differs from the separately approved queue draft")
        return {**row, **media}
    if record["format"] not in ("IMAGE", "CAROUSEL") or draft.get("media"):
        raise ValueError("Unsupported pipeline media format")
    return dict(row)
