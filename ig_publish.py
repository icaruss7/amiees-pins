#!/usr/bin/env python3
"""
Publish due rows from ig_queue.csv to Instagram as carousels.

Runs from the GitHub Action every few hours. A row goes out once its
release_at (UTC) has passed and it is not already recorded in ig_state.json.
At most MAX_PER_RUN posts go out per run and never two within MIN_GAP_HOURS,
so a backlog trickles out a day at a time instead of flooding the feed.

Needs one repository secret, IG_TOKEN: a never-expiring Meta system user token
with instagram_basic, instagram_content_publish, pages_show_list and
pages_read_engagement. Without it the script prints a note and exits cleanly,
so nothing breaks before setup is finished.

Set DRY_RUN=1 to print what would be posted without calling Meta.
Standard library only.
"""

import csv
import hashlib
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
QUEUE = os.path.join(HERE, "ig_queue.csv")
STATE = os.path.join(HERE, "ig_state.json")

API = "https://graph.facebook.com/" + os.environ.get("GRAPH_VERSION", "v26.0")
TOKEN = os.environ.get("IG_TOKEN", "").strip()
IG_USER_ID = os.environ.get("IG_USER_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false")
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "1"))
MIN_GAP_HOURS = float(os.environ.get("MIN_GAP_HOURS", "20"))   # never two posts closer than this
MAX_ATTEMPTS = 3
EXPECTED_USERNAME = os.environ.get("IG_EXPECTED_USERNAME", "amiees.com_").strip().lower()
DURABLE_GIT = os.environ.get("IG_DURABLE_GIT", "") == "1"


class MetaAPIError(RuntimeError):
    """Keep Meta's error codes available without changing existing error messages."""

    def __init__(self, message, code=None, subcode=None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode


def call(method, path, **params):
    data = urllib.parse.urlencode(params).encode()
    url = f"{API}/{path}"
    headers = {"Authorization": "Bearer " + TOKEN}
    if method == "GET":
        req = urllib.request.Request(url + "?" + data.decode(), headers=headers)
    else:
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        code = subcode = None
        try:
            error = json.loads(body)["error"]
            code = error.get("code")
            subcode = error.get("error_subcode")
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
        raise MetaAPIError(f"{method} request failed (HTTP {e.code}, Meta {code}/{subcode})",
                           code, subcode) from None
    except urllib.error.URLError:
        raise RuntimeError("Meta request failed before a response was received") from None


def username_of(ig_id):
    try:
        return call("GET", ig_id, fields="username").get("username", "")
    except RuntimeError:
        return ""


def find_ig_user():
    """Find the Instagram account this token may publish to, explaining what it sees if it fails."""
    if IG_USER_ID:
        return IG_USER_ID, username_of(IG_USER_ID) or "(from IG_USER_ID)"

    notes = []
    # 1. The token's own grants. A system user token lists the exact Instagram
    #    account ids it was given under granular_scopes.
    scopes = []
    try:
        info = call("GET", "debug_token", input_token=TOKEN).get("data", {})
        scopes = info.get("scopes", [])
        notes.append("Permissions on the token: " + (", ".join(scopes) or "none"))
        for g in info.get("granular_scopes", []):
            if g.get("scope") in ("instagram_content_publish", "instagram_basic"):
                for ig_id in g.get("target_ids", []):
                    name = username_of(ig_id)
                    if name:
                        return ig_id, name
    except RuntimeError as e:
        notes.append(f"Could not read the token's details: {e}")

    # 2. Facebook Pages the token can see, and the Instagram account linked to each.
    try:
        pages = call("GET", "me/accounts", fields="name,instagram_business_account{id,username}").get("data", [])
        for p in pages:
            acct = p.get("instagram_business_account")
            if acct:
                return acct["id"], acct.get("username", "")
        if pages:
            notes.append("Pages visible: " + ", ".join(p.get("name", "?") for p in pages)
                         + ". None of them has an Instagram account linked.")
        else:
            notes.append("No Facebook Pages are visible to this token.")
    except RuntimeError as e:
        notes.append(f"Could not list Pages: {e}")

    missing = [s for s in ("instagram_basic", "instagram_content_publish", "pages_show_list",
                           "pages_read_engagement", "business_management") if scopes and s not in scopes]
    if missing:
        notes.append("Missing permissions: " + ", ".join(missing))
    raise RuntimeError("Could not find an Instagram account to post to.\n  " + "\n  ".join(notes))


def confirmed(entry):
    return bool(entry.get("media_id") or entry.get("permalink") or
                entry.get("posted_at") or entry.get("phase") == "published")


def eligible(row, state, now):
    entry = state.get(row["post_id"], {})
    release = datetime.strptime(row["release_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return (row["status"] == "queued" and release <= now and not confirmed(entry)
            and entry.get("phase") not in ("publishing", "needs_review")
            and entry.get("attempts", 0) < MAX_ATTEMPTS)


def https_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Media must have a public HTTPS URL without embedded credentials")
    return url


def validate_row(row):
    if not row.get("post_id") or not re.fullmatch(r"[a-zA-Z0-9_-]+", row["post_id"]):
        raise ValueError("post_id must contain only letters, digits, underscores and hyphens")
    if len(row.get("caption", "")) > 2200:
        raise ValueError("Caption is longer than 2,200 characters")
    kind = (row.get("media_type") or "IMAGE").upper()
    images = [https_url(u.strip()) for u in row.get("images", "").split("|") if u.strip()]
    if kind == "REELS":
        https_url(row.get("video_url", ""))
        if images:
            raise ValueError("A Reel row must not also contain carousel images")
        if not re.fullmatch(r"[a-f0-9]{64}", row.get("video_sha256", "")):
            raise ValueError("A Reel needs the SHA-256 of the reviewed video")
    elif kind == "IMAGE":
        if not 1 <= len(images) <= 10:
            raise ValueError("An image post needs 1 to 10 images")
        if row.get("video_url"):
            raise ValueError("An image row must not contain a video URL")
    else:
        raise ValueError("media_type must be IMAGE or REELS")
    return kind, images


def fingerprint(row):
    keys = ("post_id", "caption", "images", "media_type", "video_url", "video_sha256")
    return hashlib.sha256(json.dumps({k: row.get(k, "") for k in keys},
                                   sort_keys=True).encode()).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify_video(row):
    # An immutable, content-addressed media URL is still required in production.
    # This check detects a changed asset at preparation time; it cannot lock a CDN.
    digest = hashlib.sha256()
    size = 0
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(https_url(row["video_url"]), timeout=60) as response:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > 100 * 1024 * 1024:
                raise ValueError("Reviewed video exceeds the local 100 MB limit")
            digest.update(block)
    if not size or digest.hexdigest() != row["video_sha256"]:
        raise ValueError("Hosted video does not match the reviewed video")


def checkpoint(state):
    temporary = STATE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporary, STATE)
    if DURABLE_GIT:
        def git(*args, allowed=(0,)):
            result = subprocess.run(["git", *args], cwd=HERE, capture_output=True)
            if result.returncode not in allowed:
                raise RuntimeError("Could not persist publication state to GitHub; publication stopped")
            return result.returncode
        git("add", "--", "ig_state.json")
        changed = git("diff", "--cached", "--quiet", "--", "ig_state.json", allowed=(0, 1))
        if changed:
            git("commit", "-m", "Checkpoint Instagram publication state", "--", "ig_state.json")
        # Push even when unchanged: a preceding push may have failed.
        git("push", "origin", "HEAD:main")


def wait_ready(container_id, label):
    for _ in range(30):
        status = call("GET", container_id, fields="status_code").get("status_code")
        if status in ("FINISHED", "PUBLISHED"):
            return status
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"{label}: container ended as {status}")
        time.sleep(10)
    raise RuntimeError(f"{label}: container was not ready after 5 minutes")


def publish(ig_user, row, state):
    entry = state.setdefault(row["post_id"], {"attempts": 0})
    if confirmed(entry) or entry.get("phase") in ("publishing", "needs_review"):
        return
    kind, images = validate_row(row)
    identity = fingerprint(row)
    if entry.get("fingerprint") and entry["fingerprint"] != identity:
        raise RuntimeError("Queued media or caption changed after preparation; use a new post_id")
    if entry.get("account_id") and entry["account_id"] != ig_user:
        raise RuntimeError("Prepared container belongs to a different Instagram account")
    entry["attempts"] = entry.get("attempts", 0) + 1
    if not entry.get("container_id"):
        if kind == "REELS":
            verify_video(row)
            parent = call("POST", f"{ig_user}/media", media_type="REELS",
                          video_url=row["video_url"], caption=row["caption"],
                          share_to_feed="true")["id"]
        elif len(images) == 1:
            parent = call("POST", f"{ig_user}/media", image_url=images[0], caption=row["caption"])["id"]
        else:
            children = []
            for url in images:
                cid = call("POST", f"{ig_user}/media", image_url=url, is_carousel_item="true")["id"]
                children.append(cid)
            for cid in children:
                if wait_ready(cid, row["post_id"]) != "FINISHED":
                    raise RuntimeError("Carousel child has an unexpected published status")
            parent = call("POST", f"{ig_user}/media", media_type="CAROUSEL",
                          children=",".join(children), caption=row["caption"])["id"]
        entry.update(container_id=parent, account_id=ig_user, fingerprint=identity, phase="ready")
        checkpoint(state)
    parent = entry["container_id"]
    if wait_ready(parent, row["post_id"]) == "PUBLISHED":
        entry.update(phase="needs_review", last_error="Container is already published; reconcile its media ID")
        checkpoint(state)
        raise RuntimeError(entry["last_error"])
    # Must reach durable storage before the only public side effect.
    entry.update(phase="publishing", publish_started_at=datetime.now(timezone.utc).isoformat())
    checkpoint(state)
    response = call("POST", f"{ig_user}/media_publish", creation_id=parent)
    if not response.get("id"):
        raise RuntimeError("Publication response has no media ID; reconcile before retrying")
    entry.update(media_id=response["id"], phase="published",
                 posted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    entry.pop("last_error", None)
    checkpoint(state)
    try:
        entry["permalink"] = call("GET", entry["media_id"], fields="permalink").get("permalink", "")
    except RuntimeError:
        entry["link_pending"] = True
    checkpoint(state)
    print(f"Posted {row['post_id']}: {entry.get('permalink') or entry['media_id']}")


def main():
    now = datetime.now(timezone.utc)
    with open(QUEUE, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as fh:
            state = json.load(fh)
    else:
        state = {}
    ids = [r["post_id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate post_id in ig_queue.csv")
    for row in rows:
        if row["status"] == "queued":
            validate_row(row)
    todo = [row for row in rows if eligible(row, state, now)][:MAX_PER_RUN]
    # Unresolved publication could have gone live recently: pause the entire account.
    unresolved = [key for key, entry in state.items()
                  if not confirmed(entry) and entry.get("phase") in ("publishing", "needs_review")]
    if unresolved:
        print("Publication needs reconciliation: " + ", ".join(unresolved))
        return 1
    posted_times = [datetime.fromisoformat(s["posted_at"]) for s in state.values() if s.get("posted_at")]
    if todo and posted_times and (now - max(posted_times)).total_seconds() < MIN_GAP_HOURS * 3600:
        print(f"Last post went out under {MIN_GAP_HOURS:g} hours ago, so the next one waits.")
        todo = []
    print(f"{len(todo)} post(s) due now.")
    if DRY_RUN:
        for row in todo:
            kind, images = validate_row(row)
            print(f"[dry run] {row['post_id']}: {kind}, {len(images)} images")
        return 0
    if not todo:
        return 0
    if not TOKEN:
        print("IG_TOKEN is not configured; nothing was posted.")
        return 0
    if os.environ.get("GITHUB_ACTIONS") == "true" and not DURABLE_GIT:
        raise RuntimeError("GitHub publishing requires IG_DURABLE_GIT=1")
    ig_user, username = find_ig_user()
    if not EXPECTED_USERNAME or username.lower() != EXPECTED_USERNAME:
        raise RuntimeError("Connected Instagram username does not match IG_EXPECTED_USERNAME")
    print(f"Posting as @{username}")
    failed = False
    for row in todo:
        try:
            publish(ig_user, row, state)
        except Exception as error:
            entry = state.setdefault(row["post_id"], {})
            # Preserve phase=publishing across an ambiguous response or failed checkpoint.
            entry["last_error"] = type(error).__name__ + ": publication did not complete; inspect the run"
            checkpoint(state)
            failed = True
            print(f"FAILED {row['post_id']}: {entry['last_error']}")
            break
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

