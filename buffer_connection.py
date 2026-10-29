#!/usr/bin/env python3
"""Read-only Buffer connection probe. No upload, draft, scheduling or publish API."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://api.buffer.com"
ACCOUNT = "query AmieesOrganizations { account { organizations { id } } }"
CHANNELS = """query AmieesChannels($input: ChannelsInput!) {
  channels(input: $input) { id name service isDisconnected isLocked }
}"""
MAX_RESPONSE = 2 * 1024 * 1024


class ConnectionError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def query_buffer(key, query, variables=None):
    if query not in (ACCOUNT, CHANNELS):
        raise ConnectionError("This probe only permits the two read-only connection queries")
    if not key or any(c.isspace() for c in key):
        raise ConnectionError("Set BUFFER_API_KEY to a valid secret; never put it in source code")
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    request = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key,
        "User-Agent": "Amiees-Growth-OS-connection-check"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        # Do not print request headers, response bodies, credentials or private metadata.
        raise ConnectionError(f"Buffer connection request failed (HTTP {exc.code}); check key permissions and expiry") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ConnectionError("Buffer could not be reached; no content was changed") from None
    if len(raw) > MAX_RESPONSE:
        raise ConnectionError("Buffer response exceeded the connection-check size limit")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ConnectionError("Buffer returned an unreadable connection response") from None
    if not isinstance(result, dict) or result.get("errors") or not isinstance(result.get("data"), dict):
        raise ConnectionError("Buffer rejected the read-only query; check account:read permission and API schema")
    return result["data"]


def check_connection(key, channel_id, handle, query=query_buffer):
    if not channel_id or not handle:
        raise ConnectionError("Pin the expected TikTok channel ID and handle before checking")
    expected = handle.removeprefix("@").casefold()
    account = query(key, ACCOUNT).get("account")
    organizations = account.get("organizations") if isinstance(account, dict) else None
    if not isinstance(organizations, list) or not 1 <= len(organizations) <= 20:
        raise ConnectionError("Buffer returned no usable organization list")
    found, seen_orgs = [], set()
    for organization in organizations:
        if not isinstance(organization, dict) or not isinstance(organization.get("id"), str):
            raise ConnectionError("Buffer returned an invalid organization")
        organization_id = organization["id"]
        if not organization_id or organization_id in seen_orgs:
            raise ConnectionError("Buffer returned duplicate or empty organization IDs")
        seen_orgs.add(organization_id)
        channels = query(key, CHANNELS, {"input": {"organizationId": organization_id}}).get("channels")
        if not isinstance(channels, list):
            raise ConnectionError("Buffer returned an invalid channel list")
        for channel in channels:
            if not isinstance(channel, dict):
                raise ConnectionError("Buffer returned an invalid channel")
            if channel.get("id") == channel_id:
                found.append(channel)
    if len(found) != 1:
        raise ConnectionError("Expected TikTok channel was missing or ambiguous; check the signed-in Buffer account")
    channel = found[0]
    if (channel.get("service") != "tiktok" or not isinstance(channel.get("name"), str)
            or channel["name"].removeprefix("@").casefold() != expected):
        raise ConnectionError("Pinned channel does not match the expected TikTok account")
    if channel.get("isDisconnected") is not False:
        raise ConnectionError("TikTok connection needs refreshing in Buffer before testing a draft")
    if channel.get("isLocked") is not False:
        raise ConnectionError("TikTok channel is locked or its status is unknown in Buffer")
    # Only the already expected public identity goes into Actions logs.
    return {"status": "buffer_connected", "service": "tiktok", "account": "@" + expected,
            "read_only": True, "content_changed": False,
            "live_video_publication_tested": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-id", required=True)
    parser.add_argument("--handle", required=True)
    args = parser.parse_args(argv)
    try:
        result = check_connection(os.environ.get("BUFFER_API_KEY", "").strip(), args.channel_id, args.handle)
    except ConnectionError as exc:
        print(f"Buffer check stopped: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
