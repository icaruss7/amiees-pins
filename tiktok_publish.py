#!/usr/bin/env python3
"""Schedule reviewed TikTok videos as Buffer mobile notifications.

The final sound selection and publish tap happen in TikTok. GitHub keeps a
durable queue and checkpoints before every Buffer mutation so an ambiguous
retry cannot silently schedule the same video twice. Standard library only.
"""

import csv
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
QUEUE = os.path.join(HERE, "tiktok_queue.csv")
STATE = os.path.join(HERE, "tiktok_state.json")
BUFFER_ENDPOINT = "https://api.buffer.com"
BUFFER_API_KEY = os.environ.get("BUFFER_API_KEY", "").strip()
EXPECTED_CHANNEL_ID = os.environ.get(
    "TIKTOK_BUFFER_CHANNEL_ID", "673d4900da2e3cb64c96536a"
).strip()
EXPECTED_USERNAME = os.environ.get(
    "TIKTOK_EXPECTED_USERNAME", "amiees.com"
).strip().lstrip("@").casefold()
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false")
CONNECTION_CHECK = os.environ.get("TIKTOK_BUFFER_CONNECTION_CHECK", "") == "1"
DURABLE_GIT = os.environ.get("TIKTOK_DURABLE_GIT", "") == "1"
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "1"))
MIN_GAP_HOURS = float(os.environ.get("TIKTOK_MIN_GAP_HOURS", "20"))
MAX_ATTEMPTS = 3
MAX_RESPONSE = 2 * 1024 * 1024

ACCOUNT_QUERY = """query AmieesOrganizations {
  account { organizations { id } }
}"""

CHANNELS_QUERY = """query AmieesChannels($input: ChannelsInput!) {
  channels(input: $input) { id name service isDisconnected isLocked }
}"""

CREATE_POST = """mutation CreateTikTokReminder($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess {
      post { id text status sentAt externalLink notificationStatus }
    }
    ... on MutationError { message }
  }
}"""


class BufferError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_public_https(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment):
        raise ValueError("video_url must be a public HTTPS URL without credentials or fragments")
    host = parsed.hostname.casefold()
    if host == "localhost" or host.endswith((".localhost", ".local")) or "." not in host:
        raise ValueError("Local video addresses are not allowed")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError("Private video addresses are not allowed")
    return value


def parse_bool(value, field):
    normalized = str(value).strip().casefold()
    if normalized in ("true", "1", "yes"):
        return True
    if normalized in ("false", "0", "no"):
        return False
    raise ValueError(f"{field} must be true or false")


def validate_row(row):
    post_id = row.get("post_id", "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", post_id):
        raise ValueError("post_id must contain only letters, digits, underscores and hyphens")
    caption = row.get("caption", "").strip()
    if not caption:
        raise ValueError("caption is required")
    if len(caption) > 2200:
        raise ValueError("TikTok caption is longer than 2200 characters")
    if len(re.findall(r"(?<!\w)#[\w]+", caption)) > 5:
        raise ValueError("TikTok caption contains more than five hashtags")
    if "\N{EM DASH}" in caption:
        raise ValueError("Public captions must not contain em dashes")
    video_url = row.get("video_url", "").strip()
    validate_public_https(video_url)
    if not re.search(r"\.(mp4|mov|webm)(?:$|\?)", video_url, re.IGNORECASE):
        raise ValueError("video_url must point to MP4, MOV or WEBM media")
    checksum = row.get("video_sha256", "").strip().casefold()
    if checksum and not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("video_sha256 must be blank or a 64-character SHA-256")
    parse_bool(row.get("is_ai_generated", ""), "is_ai_generated")
    if not row.get("sound_search", "").strip():
        raise ValueError("sound_search is required for the final TikTok step")
    datetime.strptime(row["release_at"], "%Y-%m-%d %H:%M:%S")
    return "VIDEO"


def fingerprint(row):
    fields = (
        "post_id", "caption", "video_url", "video_sha256",
        "is_ai_generated", "sound_search", "product_url",
    )
    payload = {key: row.get(key, "") for key in fields}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def confirmed(entry):
    return bool(entry.get("buffer_post_id") or entry.get("phase") == "notification_scheduled")


def eligible(row, state, now):
    entry = state.get(row["post_id"], {})
    release = datetime.strptime(row["release_at"], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc)
    return (row.get("status") == "queued" and release <= now and not confirmed(entry)
            and entry.get("phase") not in ("scheduling_notification", "needs_review")
            and entry.get("attempts", 0) < MAX_ATTEMPTS)


def spacing_ready(state, now):
    for entry in state.values():
        timestamp = entry.get("notification_scheduled_at")
        if not timestamp:
            continue
        scheduled = datetime.fromisoformat(timestamp)
        if (now - scheduled).total_seconds() < MIN_GAP_HOURS * 3600:
            return False
    return True


def checkpoint(state):
    temporary = STATE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, STATE)
    if DURABLE_GIT:
        def git(*arguments, allowed=(0,)):
            result = subprocess.run(["git", *arguments], cwd=HERE, capture_output=True)
            if result.returncode not in allowed:
                raise RuntimeError("Could not persist TikTok state to GitHub; scheduling stopped")
            return result.returncode

        git("add", "--", "tiktok_state.json")
        changed = git("diff", "--cached", "--quiet", "--", "tiktok_state.json", allowed=(0, 1))
        if changed:
            git("commit", "-m", "Checkpoint TikTok notification state", "--", "tiktok_state.json")
        git("push", "origin", "HEAD:main")


def buffer_json(query, variables=None):
    if not BUFFER_API_KEY or any(char.isspace() for char in BUFFER_API_KEY):
        raise BufferError("BUFFER_API_KEY is missing or invalid")
    body = json.dumps({"query": query, "variables": variables or {}},
                      separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        BUFFER_ENDPOINT, data=body, method="POST",
        headers={
            "Authorization": "Bearer " + BUFFER_API_KEY,
            "Content-Type": "application/json",
            "User-Agent": "Amiees-TikTok-Buffer-Publisher/1.0",
        })
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as error:
        raise BufferError(
            f"Buffer request failed (HTTP {error.code}); check the API key and permissions") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise BufferError("Buffer could not be reached; no reminder was submitted") from None
    if len(raw) > MAX_RESPONSE:
        raise BufferError("Buffer response exceeded the safety limit")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise BufferError("Buffer returned an unreadable response") from None
    if not isinstance(result, dict) or result.get("errors"):
        raise BufferError("Buffer rejected the request; inspect channel access and API permissions")
    data = result.get("data")
    if not isinstance(data, dict):
        raise BufferError("Buffer response did not include data")
    return data


def discover_tiktok_channel(query=buffer_json):
    account = query(ACCOUNT_QUERY).get("account")
    organizations = account.get("organizations") if isinstance(account, dict) else None
    if not isinstance(organizations, list) or not organizations:
        raise BufferError("Buffer returned no organizations")
    matches = []
    for organization in organizations:
        organization_id = organization.get("id") if isinstance(organization, dict) else None
        if not isinstance(organization_id, str) or not organization_id:
            raise BufferError("Buffer returned an invalid organization")
        channels = query(CHANNELS_QUERY, {"input": {"organizationId": organization_id}}).get("channels")
        if not isinstance(channels, list):
            raise BufferError("Buffer returned an invalid channel list")
        for channel in channels:
            if not isinstance(channel, dict):
                raise BufferError("Buffer returned an invalid channel")
            name = str(channel.get("name", "")).lstrip("@").casefold()
            if (channel.get("service") == "tiktok" and name == EXPECTED_USERNAME
                    and channel.get("id") == EXPECTED_CHANNEL_ID):
                matches.append(channel)
    if len(matches) != 1:
        raise BufferError("Expected TikTok channel was missing or ambiguous in Buffer")
    channel = matches[0]
    if channel.get("isDisconnected") is not False:
        raise BufferError("The TikTok channel needs reconnecting in Buffer")
    if channel.get("isLocked") is not False:
        raise BufferError("The TikTok channel is locked by the current Buffer plan")
    return channel["id"], str(channel.get("name", "")).lstrip("@")


def post_input(row, channel_id):
    validate_row(row)
    return {
        "text": row["caption"].strip(),
        "channelId": channel_id,
        "schedulingType": "notification",
        "mode": "shareNow",
        "assets": [{"video": {"url": row["video_url"].strip()}}],
        "metadata": {
            "tiktok": {
                "isAiGenerated": parse_bool(
                    row.get("is_ai_generated", ""), "is_ai_generated")
            }
        },
    }


def schedule_notification(row, state, channel_id):
    entry = state.setdefault(row["post_id"], {"attempts": 0})
    if confirmed(entry) or entry.get("phase") in ("scheduling_notification", "needs_review"):
        return
    identity = fingerprint(row)
    if entry.get("fingerprint") and entry["fingerprint"] != identity:
        raise RuntimeError("Queued video or caption changed after preparation; use a new post_id")
    entry["attempts"] = entry.get("attempts", 0) + 1
    entry["fingerprint"] = identity
    entry.update(
        phase="scheduling_notification",
        scheduling_started_at=datetime.now(timezone.utc).isoformat(),
        transport="buffer_notification",
        sound_search=row["sound_search"].strip(),
    )
    checkpoint(state)
    data = buffer_json(CREATE_POST, {"input": post_input(row, channel_id)})
    outcome = data.get("createPost")
    if not isinstance(outcome, dict):
        raise BufferError("Buffer did not return a createPost result")
    if outcome.get("__typename") != "PostActionSuccess":
        raise BufferError("Buffer rejected the reminder: " + str(outcome.get("message", "unknown error"))[:300])
    post = outcome.get("post")
    buffer_post_id = str(post.get("id", "")) if isinstance(post, dict) else ""
    if not buffer_post_id:
        raise BufferError("Buffer accepted no post ID; reconcile before retrying")
    entry.update(
        phase="notification_scheduled",
        buffer_post_id=buffer_post_id,
        notification_scheduled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    entry.pop("last_error", None)
    checkpoint(state)
    print(f"Scheduled TikTok notification for {row['post_id']}.")
    print("Sound search: " + row["sound_search"].strip())


def main():
    now = datetime.now(timezone.utc)
    with open(QUEUE, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    with open(STATE, encoding="utf-8") as handle:
        state = json.load(handle)

    ids = [row.get("post_id", "") for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate post_id in tiktok_queue.csv")
    for row in rows:
        if row.get("status") == "queued":
            validate_row(row)

    unresolved = [
        key for key, entry in state.items()
        if not confirmed(entry) and entry.get("phase") in ("scheduling_notification", "needs_review")
    ]
    if unresolved:
        print("TikTok notification needs reconciliation: " + ", ".join(unresolved))
        return 1

    due = sorted(
        (row for row in rows if eligible(row, state, now)),
        key=lambda row: (row["release_at"], row["post_id"]),
    )
    todo = due[:max(0, MAX_PER_RUN)] if spacing_ready(state, now) else []
    if due and not todo:
        print("Due TikTok reminders are waiting for the spacing window.")
    print(f"{len(todo)} TikTok reminder(s) due now.")

    if DRY_RUN:
        if CONNECTION_CHECK:
            if BUFFER_API_KEY:
                channel_id, name = discover_tiktok_channel()
                marker = {
                    "phase": "connected",
                    "transport": "buffer_notification",
                    "channel_id": channel_id,
                    "username": name,
                }
                if state.get("_buffer_connection") != marker:
                    state["_buffer_connection"] = marker
                    checkpoint(state)
                print(f"Connected Buffer TikTok channel: @{name}")
            else:
                print("Buffer credentials are not configured; connection check skipped.")
        for row in todo:
            print(f"[dry run] {row['post_id']}: {validate_row(row)}")
            print("[dry run] sound search: " + row["sound_search"].strip())
        return 0

    if not BUFFER_API_KEY:
        print("BUFFER_API_KEY is not configured; TikTok reminders remain disabled.")
        return 0

    channel = discover_tiktok_channel() if (CONNECTION_CHECK or todo) else None
    if channel:
        print(f"Connected Buffer TikTok channel: @{channel[1]}")
    if not todo:
        return 0
    if os.environ.get("GITHUB_ACTIONS") == "true" and not DURABLE_GIT:
        raise RuntimeError("GitHub scheduling requires TIKTOK_DURABLE_GIT=1")

    for row in todo:
        try:
            schedule_notification(row, state, channel[0])
        except Exception as error:
            entry = state.setdefault(row["post_id"], {})
            if entry.get("phase") == "scheduling_notification":
                entry["phase"] = "needs_review"
            entry["last_error"] = type(error).__name__ + ": reminder did not complete; inspect the run"
            checkpoint(state)
            print(f"FAILED {row['post_id']}: {entry['last_error']}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
