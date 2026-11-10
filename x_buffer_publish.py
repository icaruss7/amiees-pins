#!/usr/bin/env python3
"""Publish reviewed X posts through Buffer's free API.

GitHub keeps the queue and durable state. Buffer handles the X API connection,
so this workflow does not require X API credits. Standard library only.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

import x_publish as core

BUFFER_ENDPOINT = "https://api.buffer.com"
BUFFER_API_KEY = os.environ.get("BUFFER_API_KEY", "").strip()
EXPECTED_USERNAME = os.environ.get("X_EXPECTED_USERNAME", "").strip().lstrip("@").casefold()
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false")
CONNECTION_CHECK = os.environ.get("X_BUFFER_CONNECTION_CHECK", "") == "1"
MAX_RESPONSE = 2 * 1024 * 1024

ACCOUNT_QUERY = """query AmieesOrganizations {
  account { organizations { id } }
}"""

CHANNELS_QUERY = """query AmieesChannels($input: ChannelsInput!) {
  channels(input: $input) { id name service isDisconnected isLocked }
}"""

CREATE_POST = """mutation CreateXPost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess {
      post { id text status sentAt externalLink }
    }
    ... on MutationError { message }
  }
}"""


class BufferError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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
            "User-Agent": "Amiees-X-Buffer-Publisher/1.0",
        })
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as error:
        raise BufferError(
            f"Buffer request failed (HTTP {error.code}); check the API key and permissions") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise BufferError("Buffer could not be reached; no post was submitted") from None
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


def discover_x_channel(query=buffer_json):
    if not EXPECTED_USERNAME:
        raise BufferError("X_EXPECTED_USERNAME is required")
    account = query(ACCOUNT_QUERY).get("account")
    organizations = account.get("organizations") if isinstance(account, dict) else None
    if not isinstance(organizations, list) or not organizations:
        raise BufferError("Buffer returned no organizations")

    matches = []
    seen = set()
    for organization in organizations:
        organization_id = organization.get("id") if isinstance(organization, dict) else None
        if not isinstance(organization_id, str) or not organization_id or organization_id in seen:
            raise BufferError("Buffer returned an invalid organization")
        seen.add(organization_id)
        channels = query(CHANNELS_QUERY, {"input": {"organizationId": organization_id}}).get("channels")
        if not isinstance(channels, list):
            raise BufferError("Buffer returned an invalid channel list")
        for channel in channels:
            if not isinstance(channel, dict):
                raise BufferError("Buffer returned an invalid channel")
            name = str(channel.get("name", "")).lstrip("@").casefold()
            if channel.get("service") == "twitter" and name == EXPECTED_USERNAME:
                matches.append(channel)

    if len(matches) != 1:
        raise BufferError(
            "Expected X channel was missing or ambiguous; connect @"
            + EXPECTED_USERNAME + " in Buffer")
    channel = matches[0]
    if channel.get("isDisconnected") is not False:
        raise BufferError("The X channel needs reconnecting in Buffer")
    if channel.get("isLocked") is not False:
        raise BufferError("The X channel is locked by the current Buffer plan")
    channel_id = channel.get("id")
    if not isinstance(channel_id, str) or not channel_id:
        raise BufferError("Buffer X channel did not include an ID")
    return channel_id, str(channel.get("name", "")).lstrip("@")


def post_input(row, channel_id):
    kind = core.validate_row(row)
    result = {
        "text": row["text"].strip(),
        "channelId": channel_id,
        "schedulingType": "automatic",
        "mode": "shareNow",
    }
    if kind == "IMAGE":
        image = {"url": row["image_url"].strip()}
        alt_text = row.get("alt_text", "").strip()
        if alt_text:
            image["metadata"] = {"altText": alt_text}
        result["assets"] = [{"image": image}]
    return result


def submit(row, state, channel_id):
    entry = state.setdefault(row["post_id"], {"attempts": 0})
    if core.confirmed(entry) or entry.get("phase") in ("publishing", "needs_review"):
        return

    identity = core.fingerprint(row)
    if entry.get("fingerprint") and entry["fingerprint"] != identity:
        raise RuntimeError("Queued media or text changed after preparation; use a new post_id")
    entry["attempts"] = entry.get("attempts", 0) + 1
    entry["fingerprint"] = identity
    entry.update(
        phase="publishing",
        publish_started_at=datetime.now(timezone.utc).isoformat(),
        transport="buffer",
    )
    core.checkpoint(state)

    data = buffer_json(CREATE_POST, {"input": post_input(row, channel_id)})
    outcome = data.get("createPost")
    if not isinstance(outcome, dict):
        raise BufferError("Buffer did not return a createPost result")
    if outcome.get("__typename") != "PostActionSuccess":
        raise BufferError("Buffer rejected the post: " + str(outcome.get("message", "unknown error"))[:300])
    post = outcome.get("post")
    buffer_post_id = str(post.get("id", "")) if isinstance(post, dict) else ""
    if not buffer_post_id:
        raise BufferError("Buffer accepted no post ID; reconcile before retrying")

    entry.update(
        phase="published",
        buffer_post_id=buffer_post_id,
        posted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    external_link = post.get("externalLink") if isinstance(post, dict) else None
    if external_link:
        entry["permalink"] = external_link
    entry.pop("last_error", None)
    core.checkpoint(state)
    print(f"Submitted {row['post_id']} to Buffer for X publication.")


def main():
    now = datetime.now(timezone.utc)
    with open(core.QUEUE, encoding="utf-8-sig", newline="") as handle:
        rows = list(core.csv.DictReader(handle))
    with open(core.STATE, encoding="utf-8") as handle:
        state = json.load(handle)

    ids = [row.get("post_id", "") for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate post_id in x_queue.csv")
    for row in rows:
        if row.get("status") == "queued":
            core.validate_row(row)

    unresolved = [
        key for key, entry in state.items()
        if not core.confirmed(entry) and entry.get("phase") in ("publishing", "needs_review")
    ]
    if unresolved:
        print("X publication needs reconciliation: " + ", ".join(unresolved))
        return 1

    due = sorted(
        (row for row in rows if core.eligible(row, state, now)),
        key=lambda row: (row["release_at"], row["post_id"]),
    )
    todo = due[:max(0, core.MAX_PER_RUN)] if core.spacing_ready(state, now) else []
    if due and not todo:
        print("Due X posts are waiting for the spacing window.")
    print(f"{len(todo)} X post(s) due now.")

    if DRY_RUN:
        if CONNECTION_CHECK:
            if BUFFER_API_KEY:
                _, name = discover_x_channel()
                print(f"Connected Buffer X channel: @{name}")
            else:
                print("Buffer credentials are not configured; connection check skipped.")
        for row in todo:
            print(f"[dry run] {row['post_id']}: {core.validate_row(row)}")
        return 0

    if not BUFFER_API_KEY:
        print("BUFFER_API_KEY is not configured; publishing remains disabled.")
        return 0

    channel = None
    if CONNECTION_CHECK or todo:
        channel = discover_x_channel()
        print(f"Connected Buffer X channel: @{channel[1]}")
    if not todo:
        return 0
    if os.environ.get("GITHUB_ACTIONS") == "true" and not core.DURABLE_GIT:
        raise RuntimeError("GitHub publishing requires X_DURABLE_GIT=1")

    for row in todo:
        try:
            submit(row, state, channel[0])
        except Exception as error:
            entry = state.setdefault(row["post_id"], {})
            if entry.get("phase") == "publishing":
                entry["phase"] = "needs_review"
            entry["last_error"] = type(error).__name__ + ": publication did not complete; inspect the run"
            core.checkpoint(state)
            print(f"FAILED {row['post_id']}: {entry['last_error']}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
