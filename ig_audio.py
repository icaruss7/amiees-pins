#!/usr/bin/env python3
"""Read Instagram's native trending music catalog. Never create or publish media."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import ig_publish


def catalog(request, user_id):
    response = request("GET", "ig_audio", audio_type="music", user_id=user_id)
    tracks = response.get("audio")
    if not isinstance(tracks, list):
        raise ValueError("Instagram returned no audio catalog")
    keep = ("audio_id", "title", "display_artist", "duration_in_ms", "audio_type",
            "is_ads_eligible", "on_platform_audio_preview_link")
    return [{key: track[key] for key in keep if key in track}
            for track in tracks if isinstance(track, dict)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audio-id", action="append", default=[])
    args = parser.parse_args()
    if not ig_publish.TOKEN:
        raise ValueError("IG_TOKEN is required; no content was changed")
    user_id, username = ig_publish.find_ig_user()
    if username.lower() != ig_publish.EXPECTED_USERNAME:
        raise ValueError("Connected Instagram account does not match Amiees")
    tracks = catalog(ig_publish.call, user_id)
    metadata = []
    for audio_id in args.audio_id:
        if not audio_id.isdigit():
            raise ValueError("Audio IDs must be numeric")
        track = ig_publish.call("GET", audio_id, user_id=user_id)
        keep = ("audio_id", "title", "display_artist", "duration_in_ms", "audio_type",
                "is_ads_eligible", "on_platform_audio_preview_link")
        metadata.append({key: track[key] for key in keep if key in track})
    result = {"collected_at": datetime.now(timezone.utc).isoformat(),
              "username": username, "source": "Meta Instagram Audio API",
              "selection": "Trending music returned without search_query",
              "commercial_review_required": True, "tracks": tracks,
              "requested_metadata": metadata}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
