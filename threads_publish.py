#!/usr/bin/env python3
"""Publish reviewed Threads posts from threads_queue.csv.

Runs in GitHub Actions. Publication state is committed to GitHub before the
public side effect, preventing an ambiguous retry from creating duplicates.
Standard library only.
"""

import csv
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
QUEUE = os.path.join(HERE, "threads_queue.csv")
STATE = os.path.join(HERE, "threads_state.json")
API = "https://graph.threads.net/" + os.environ.get("THREADS_API_VERSION", "v1.0")
TOKEN = os.environ.get("THREADS_TOKEN", "").strip()
USER_ID = os.environ.get("THREADS_USER_ID", "").strip()
EXPECTED_USERNAME = os.environ.get("THREADS_EXPECTED_USERNAME", "").strip().lower()
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false")
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "1"))
MIN_GAP_HOURS = float(os.environ.get("THREADS_MIN_GAP_HOURS", "16"))
DURABLE_GIT = os.environ.get("THREADS_DURABLE_GIT", "") == "1"
MAX_ATTEMPTS = 3


class ThreadsAPIError(RuntimeError):
    pass


def call(method, path, **params):
    params["access_token"] = TOKEN
    data = urllib.parse.urlencode(params).encode()
    url = f"{API}/{path.lstrip('/')}"
    if method == "GET":
        request = urllib.request.Request(url + "?" + data.decode(), method="GET")
    else:
        request = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise ThreadsAPIError(f"Threads API HTTP {error.code}: {body[:500]}") from None
    except urllib.error.URLError:
        raise ThreadsAPIError("Threads API request failed before a response was received") from None


def https_url(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Image media must use a public HTTPS URL without credentials or fragments")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local")) or "." not in host:
        raise ValueError("Local image addresses are not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Private image addresses are not allowed")
    return value


def validate_row(row):
    post_id = row.get("post_id", "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", post_id):
        raise ValueError("post_id must contain only letters, digits, underscores and hyphens")
    text = row.get("text", "").strip()
    if not text:
        raise ValueError("text is required")
    if len(text) > 500:
        raise ValueError("Threads text is longer than 500 characters")
    kind = (row.get("media_type") or "TEXT").upper()
    if kind not in ("TEXT", "IMAGE"):
        raise ValueError("media_type must be TEXT or IMAGE")
    if kind == "IMAGE":
        https_url(row.get("image_url", ""))
    elif row.get("image_url", "").strip():
        raise ValueError("A TEXT row must not contain image_url")
    datetime.strptime(row["release_at"], "%Y-%m-%d %H:%M:%S")
    return kind


def fingerprint(row):
    fields = ("post_id", "text", "media_type", "image_url", "alt_text", "product_url")
    return hashlib.sha256(json.dumps({key: row.get(key, "") for key in fields},
                                     sort_keys=True).encode()).hexdigest()


def confirmed(entry):
    return bool(entry.get("thread_id") or entry.get("posted_at") or entry.get("phase") == "published")


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
                raise RuntimeError("Could not persist Threads state to GitHub; publication stopped")
            return result.returncode

        git("add", "--", "threads_state.json")
        changed = git("diff", "--cached", "--quiet", "--", "threads_state.json", allowed=(0, 1))
        if changed:
            git("commit", "-m", "Checkpoint Threads publication state", "--", "threads_state.json")
        git("push", "origin", "HEAD:main")


def account_identity():
    profile = call("GET", "me", fields="id,username")
    profile_id = str(profile.get("id", ""))
    username = profile.get("username", "")
    if not profile_id:
        raise RuntimeError("Threads profile response did not include an ID")
    if USER_ID and profile_id != USER_ID:
        raise RuntimeError("THREADS_USER_ID does not match the connected access token")
    if EXPECTED_USERNAME and username.lower() != EXPECTED_USERNAME:
        raise RuntimeError("Connected Threads username does not match THREADS_EXPECTED_USERNAME")
    return profile_id, username


def wait_ready(container_id, label):
    for _ in range(30):
        result = call("GET", container_id, fields="status,error_message")
        status = result.get("status")
        if status in ("FINISHED", "PUBLISHED"):
            return status
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"{label}: container ended as {status}: {result.get('error_message', '')}")
        time.sleep(5)
    raise RuntimeError(f"{label}: container was not ready after 150 seconds")


def publish(row, state, user_id=None):
    entry = state.setdefault(row["post_id"], {"attempts": 0})
    if confirmed(entry) or entry.get("phase") in ("publishing", "needs_review"):
        return
    kind = validate_row(row)
    identity = fingerprint(row)
    if entry.get("fingerprint") and entry["fingerprint"] != identity:
        raise RuntimeError("Queued media or text changed after preparation; use a new post_id")
    entry["attempts"] = entry.get("attempts", 0) + 1
    if not entry.get("container_id"):
        options = {"media_type": kind, "text": row["text"].strip()}
        if kind == "IMAGE":
            options["image_url"] = row["image_url"].strip()
            if row.get("alt_text", "").strip():
                options["alt_text"] = row["alt_text"].strip()
        target_id = user_id or USER_ID
        if not target_id:
            raise RuntimeError("Threads user ID was not resolved")
        container_id = call("POST", f"{target_id}/threads", **options).get("id")
        if not container_id:
            raise RuntimeError("Threads container response did not include an ID")
        entry.update(container_id=container_id, fingerprint=identity, phase="ready")
        checkpoint(state)

    status = wait_ready(entry["container_id"], row["post_id"])
    if status == "PUBLISHED":
        entry.update(phase="needs_review", last_error="Container is already published; reconcile its thread ID")
        checkpoint(state)
        raise RuntimeError(entry["last_error"])

    entry.update(phase="publishing", publish_started_at=datetime.now(timezone.utc).isoformat())
    checkpoint(state)
    target_id = user_id or USER_ID
    result = call("POST", f"{target_id}/threads_publish", creation_id=entry["container_id"])
    thread_id = result.get("id")
    if not thread_id:
        raise RuntimeError("Publication response has no thread ID; reconcile before retrying")
    entry.update(thread_id=thread_id, phase="published",
                 posted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    entry.pop("last_error", None)
    checkpoint(state)
    try:
        entry["permalink"] = call("GET", thread_id, fields="permalink").get("permalink", "")
    except ThreadsAPIError:
        entry["link_pending"] = True
    checkpoint(state)
    print(f"Posted {row['post_id']}: {entry.get('permalink') or thread_id}")


def main():
    now = datetime.now(timezone.utc)
    with open(QUEUE, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    with open(STATE, encoding="utf-8") as handle:
        state = json.load(handle)

    ids = [row.get("post_id", "") for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate post_id in threads_queue.csv")
    for row in rows:
        if row.get("status") == "queued":
            validate_row(row)

    unresolved = [key for key, entry in state.items()
                  if not confirmed(entry) and entry.get("phase") in ("publishing", "needs_review")]
    if unresolved:
        print("Threads publication needs reconciliation: " + ", ".join(unresolved))
        return 1

    due = sorted((row for row in rows if eligible(row, state, now)),
                 key=lambda row: (row["release_at"], row["post_id"]))
    todo = due[:max(0, MAX_PER_RUN)] if spacing_ready(state, now) else []
    if due and not todo:
        print("Due Threads posts are waiting for the spacing window.")
    print(f"{len(todo)} Threads post(s) due now.")
    if DRY_RUN:
        for row in todo:
            print(f"[dry run] {row['post_id']}: {validate_row(row)}")
        return 0
    if not todo:
        return 0
    if not TOKEN:
        print("THREADS_TOKEN is not configured; nothing was posted.")
        return 0
    if os.environ.get("GITHUB_ACTIONS") == "true" and not DURABLE_GIT:
        raise RuntimeError("GitHub publishing requires THREADS_DURABLE_GIT=1")

    user_id, username = account_identity()
    print(f"Posting to Threads as @{username}")
    for row in todo:
        try:
            publish(row, state, user_id)
        except Exception as error:
            entry = state.setdefault(row["post_id"], {})
            entry["last_error"] = type(error).__name__ + ": publication did not complete; inspect the run"
            checkpoint(state)
            print(f"FAILED {row['post_id']}: {entry['last_error']}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
