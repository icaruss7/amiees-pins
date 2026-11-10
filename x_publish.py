#!/usr/bin/env python3
"""Publish reviewed X posts from x_queue.csv.

Designed for GitHub Actions and X API user-context authentication. Publication
state is committed before the public API call so an ambiguous retry cannot
silently create a duplicate. Standard library only.
"""

import base64
import csv
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
QUEUE = os.path.join(HERE, "x_queue.csv")
STATE = os.path.join(HERE, "x_state.json")
API_KEY = os.environ.get("X_API_KEY", "").strip()
API_SECRET = os.environ.get("X_API_SECRET", "").strip()
ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN", "").strip()
ACCESS_TOKEN_SECRET = os.environ.get("X_ACCESS_TOKEN_SECRET", "").strip()
EXPECTED_USERNAME = os.environ.get("X_EXPECTED_USERNAME", "").strip().lstrip("@").lower()
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false")
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "1"))
MIN_GAP_HOURS = float(os.environ.get("X_MIN_GAP_HOURS", "20"))
DURABLE_GIT = os.environ.get("X_DURABLE_GIT", "") == "1"
CONNECTION_CHECK = os.environ.get("X_CONNECTION_CHECK", "") == "1"
MAX_ATTEMPTS = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


class XAPIError(RuntimeError):
    pass


def credentials_ready():
    return all((API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET))


def percent(value):
    return urllib.parse.quote(str(value), safe="~-._")


def oauth_header(method, url, query=None, nonce=None, timestamp=None):
    """Build an OAuth 1.0a HMAC-SHA1 Authorization header."""
    oauth = {
        "oauth_consumer_key": API_KEY,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(timestamp or int(time.time())),
        "oauth_token": ACCESS_TOKEN,
        "oauth_version": "1.0",
    }
    parsed = urllib.parse.urlsplit(url)
    params = list(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    params.extend((query or {}).items())
    params.extend(oauth.items())
    normalized = "&".join(f"{percent(k)}={percent(v)}" for k, v in sorted(params))
    base_url = urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", ""))
    base = "&".join((method.upper(), percent(base_url), percent(normalized)))
    key = f"{percent(API_SECRET)}&{percent(ACCESS_TOKEN_SECRET)}".encode()
    oauth["oauth_signature"] = base64.b64encode(
        hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(
        f'{percent(k)}="{percent(v)}"' for k, v in sorted(oauth.items()))


def api_json(method, url, payload=None, query=None):
    query = query or {}
    if query:
        separator = "&" if urllib.parse.urlsplit(url).query else "?"
        url = url + separator + urllib.parse.urlencode(query)
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(url, data=body, method=method.upper())
    request.add_header("Authorization", oauth_header(method, url))
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        body_text = error.read().decode("utf-8", errors="replace")
        raise XAPIError(f"X API HTTP {error.code}: {body_text[:500]}") from None
    except urllib.error.URLError:
        raise XAPIError("X API request failed before a response was received") from None


def validate_public_https(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Image media must use a public HTTPS URL without credentials or fragments")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local")) or "." not in host:
        raise ValueError("Local image addresses are not allowed")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError("Private image addresses are not allowed")
    return value


def require_public_dns(value):
    host = urllib.parse.urlsplit(value).hostname
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("Image host could not be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Private image addresses are not allowed")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_image(url):
    opener = urllib.request.build_opener(NoRedirect)
    current = url
    for _ in range(4):
        validate_public_https(current)
        require_public_dns(current)
        request = urllib.request.Request(current, headers={"User-Agent": "Amiees-X-Publisher/1.0"})
        try:
            response = opener.open(request, timeout=60)
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308) and error.headers.get("Location"):
                current = urllib.parse.urljoin(current, error.headers["Location"])
                continue
            raise XAPIError(f"Image download HTTP {error.code}") from None
        with response:
            content_type = response.headers.get_content_type().lower()
            if content_type not in ALLOWED_IMAGE_TYPES:
                raise ValueError(f"Unsupported image content type: {content_type}")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_IMAGE_BYTES:
                raise ValueError("Image is larger than X's 5 MB single-image limit")
            data = response.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("Image is larger than X's 5 MB single-image limit")
            return data, content_type
    raise XAPIError("Too many redirects while downloading the image")


def weighted_length(text):
    """Conservative X length estimate: URLs count 23 and non-BMP emoji count 2."""
    total = 0
    cursor = 0
    for match in re.finditer(r"https?://[^\s]+", text):
        total += sum(2 if ord(ch) > 0xFFFF else 1 for ch in text[cursor:match.start()])
        total += 23
        cursor = match.end()
    total += sum(2 if ord(ch) > 0xFFFF else 1 for ch in text[cursor:])
    return total


def validate_row(row):
    post_id = row.get("post_id", "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", post_id):
        raise ValueError("post_id must contain only letters, digits, underscores and hyphens")
    text = row.get("text", "").strip()
    if not text:
        raise ValueError("text is required")
    if weighted_length(text) > 280:
        raise ValueError("X post is longer than 280 weighted characters")
    kind = (row.get("media_type") or "TEXT").upper()
    if kind not in ("TEXT", "IMAGE"):
        raise ValueError("media_type must be TEXT or IMAGE")
    if kind == "IMAGE":
        validate_public_https(row.get("image_url", ""))
        if len(row.get("alt_text", "")) > 1000:
            raise ValueError("alt_text is longer than 1000 characters")
    elif row.get("image_url", "").strip():
        raise ValueError("A TEXT row must not contain image_url")
    datetime.strptime(row["release_at"], "%Y-%m-%d %H:%M:%S")
    return kind


def fingerprint(row):
    fields = ("post_id", "text", "media_type", "image_url", "alt_text", "product_url")
    return hashlib.sha256(json.dumps({key: row.get(key, "") for key in fields},
                                     sort_keys=True).encode()).hexdigest()


def confirmed(entry):
    return bool(entry.get("x_post_id") or entry.get("posted_at") or entry.get("phase") == "published")


def eligible(row, state, now):
    entry = state.get(row["post_id"], {})
    release = datetime.strptime(row["release_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return (row.get("status") == "queued" and release <= now and not confirmed(entry)
            and entry.get("phase") not in ("publishing", "needs_review")
            and entry.get("attempts", 0) < MAX_ATTEMPTS)


def spacing_ready(state, now):
    for entry in state.values():
        if not entry.get("posted_at"):
            continue
        posted = datetime.fromisoformat(entry["posted_at"])
        if (now - posted).total_seconds() < MIN_GAP_HOURS * 3600:
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
                raise RuntimeError("Could not persist X state to GitHub; publication stopped")
            return result.returncode

        git("add", "--", "x_state.json")
        changed = git("diff", "--cached", "--quiet", "--", "x_state.json", allowed=(0, 1))
        if changed:
            git("commit", "-m", "Checkpoint X publication state", "--", "x_state.json")
        git("push", "origin", "HEAD:main")


def account_identity():
    result = api_json("GET", "https://api.x.com/2/users/me",
                      query={"user.fields": "id,name,username,description,url,profile_image_url"})
    profile = result.get("data", {})
    profile_id = str(profile.get("id", ""))
    username = profile.get("username", "")
    if not profile_id or not username:
        raise RuntimeError("X profile response did not include an ID and username")
    if EXPECTED_USERNAME and username.lower() != EXPECTED_USERNAME:
        raise RuntimeError("Connected X username does not match X_EXPECTED_USERNAME")
    return profile_id, username


def upload_media(row):
    data, _ = download_image(row["image_url"].strip())
    result = api_json("POST", "https://api.x.com/2/media/upload", {
        "media": base64.b64encode(data).decode(),
        "media_category": "tweet_image",
    })
    media_id = str(result.get("data", {}).get("id", ""))
    if not media_id:
        raise RuntimeError("X media upload response did not include an ID")
    alt_text = row.get("alt_text", "").strip()
    if alt_text:
        try:
            api_json("POST", "https://upload.twitter.com/1.1/media/metadata/create.json", {
                "media_id": media_id,
                "alt_text": {"text": alt_text},
            })
        except XAPIError as error:
            raise RuntimeError("Image uploaded, but alt text could not be attached") from error
    return media_id


def publish(row, state, username):
    entry = state.setdefault(row["post_id"], {"attempts": 0})
    if confirmed(entry) or entry.get("phase") in ("publishing", "needs_review"):
        return
    kind = validate_row(row)
    identity = fingerprint(row)
    if entry.get("fingerprint") and entry["fingerprint"] != identity:
        raise RuntimeError("Queued media or text changed after preparation; use a new post_id")
    entry["attempts"] = entry.get("attempts", 0) + 1
    entry["fingerprint"] = identity

    if kind == "IMAGE" and not entry.get("media_id"):
        entry["media_id"] = upload_media(row)
        entry["phase"] = "media_uploaded"
        checkpoint(state)

    payload = {"text": row["text"].strip()}
    if entry.get("media_id"):
        payload["media"] = {"media_ids": [entry["media_id"]]}
    entry.update(phase="publishing", publish_started_at=datetime.now(timezone.utc).isoformat())
    checkpoint(state)
    result = api_json("POST", "https://api.x.com/2/tweets", payload)
    post_id = str(result.get("data", {}).get("id", ""))
    if not post_id:
        raise RuntimeError("X publication response has no post ID; reconcile before retrying")
    entry.update(x_post_id=post_id, phase="published",
                 posted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 permalink=f"https://x.com/{username}/status/{post_id}")
    entry.pop("last_error", None)
    checkpoint(state)
    print(f"Posted {row['post_id']}: {entry['permalink']}")


def main():
    now = datetime.now(timezone.utc)
    with open(QUEUE, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    with open(STATE, encoding="utf-8") as handle:
        state = json.load(handle)

    ids = [row.get("post_id", "") for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate post_id in x_queue.csv")
    for row in rows:
        if row.get("status") == "queued":
            validate_row(row)

    unresolved = [key for key, entry in state.items()
                  if not confirmed(entry) and entry.get("phase") in ("publishing", "needs_review")]
    if unresolved:
        print("X publication needs reconciliation: " + ", ".join(unresolved))
        return 1

    due = sorted((row for row in rows if eligible(row, state, now)),
                 key=lambda row: (row["release_at"], row["post_id"]))
    todo = due[:max(0, MAX_PER_RUN)] if spacing_ready(state, now) else []
    if due and not todo:
        print("Due X posts are waiting for the spacing window.")
    print(f"{len(todo)} X post(s) due now.")
    if DRY_RUN:
        if CONNECTION_CHECK:
            if credentials_ready():
                profile_id, username = account_identity()
                print(f"Connected X account: @{username} (ID {profile_id})")
            else:
                print("X credentials are not configured; connection check skipped.")
        for row in todo:
            print(f"[dry run] {row['post_id']}: {validate_row(row)}")
        return 0
    if not credentials_ready():
        print("X credentials are not configured; publishing remains disabled.")
        return 0

    connected_account = None
    if CONNECTION_CHECK or todo:
        connected_account = account_identity()
        print(f"Connected X account: @{connected_account[1]} (ID {connected_account[0]})")
    if not todo:
        return 0
    if os.environ.get("GITHUB_ACTIONS") == "true" and not DURABLE_GIT:
        raise RuntimeError("GitHub publishing requires X_DURABLE_GIT=1")

    _, username = connected_account
    for row in todo:
        try:
            publish(row, state, username)
        except Exception as error:
            entry = state.setdefault(row["post_id"], {})
            if entry.get("phase") == "publishing":
                entry["phase"] = "needs_review"
            entry["last_error"] = type(error).__name__ + ": publication did not complete; inspect the run"
            checkpoint(state)
            print(f"FAILED {row['post_id']}: {entry['last_error']}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
