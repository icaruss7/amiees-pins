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
import json
import os
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


def call(method, path, **params):
    params["access_token"] = TOKEN
    data = urllib.parse.urlencode(params).encode()
    url = f"{API}/{path}"
    if method == "GET":
        req = urllib.request.Request(url + "?" + data.decode())
    else:
        req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} failed ({e.code}): {body}") from None


def find_ig_user():
    if IG_USER_ID:
        return IG_USER_ID, "(from IG_USER_ID)"
    pages = call("GET", "me/accounts", fields="name,instagram_business_account{id,username}")
    for p in pages.get("data", []):
        acct = p.get("instagram_business_account")
        if acct:
            return acct["id"], acct.get("username", "")
    raise RuntimeError("The token can see no Facebook Page with a linked Instagram business account.")


def wait_ready(container_id, label):
    for _ in range(30):                      # up to about 5 minutes
        status = call("GET", container_id, fields="status_code").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"{label}: container {container_id} ended as {status}")
        time.sleep(10)
    raise RuntimeError(f"{label}: container {container_id} not ready after 5 minutes")


def publish(ig_user, row):
    images = [u.strip() for u in row["images"].split("|") if u.strip()]
    if len(images) == 1:
        parent = call("POST", f"{ig_user}/media", image_url=images[0], caption=row["caption"])["id"]
    else:
        children = []
        for url in images[:10]:
            cid = call("POST", f"{ig_user}/media", image_url=url, is_carousel_item="true")["id"]
            children.append(cid)
        for cid in children:
            wait_ready(cid, row["post_id"])
        parent = call("POST", f"{ig_user}/media", media_type="CAROUSEL",
                      children=",".join(children), caption=row["caption"])["id"]
    wait_ready(parent, row["post_id"])
    media_id = call("POST", f"{ig_user}/media_publish", creation_id=parent)["id"]
    link = call("GET", media_id, fields="permalink").get("permalink", "")
    return media_id, link


def main():
    now = datetime.now(timezone.utc)
    with open(QUEUE, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}

    ids = [r["post_id"] for r in rows]
    if len(ids) != len(set(ids)):
        sys.exit("duplicate post_id in ig_queue.csv")

    def due(r):
        rel = datetime.strptime(r["release_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        s = state.get(r["post_id"], {})
        return (r["status"] == "queued" and rel <= now and not s.get("media_id")
                and s.get("attempts", 0) < MAX_ATTEMPTS)

    todo = [r for r in rows if due(r)][:MAX_PER_RUN]
    posted_times = [datetime.fromisoformat(s["posted_at"]) for s in state.values() if s.get("posted_at")]
    if todo and posted_times and (now - max(posted_times)).total_seconds() < MIN_GAP_HOURS * 3600:
        print(f"Last post went out under {MIN_GAP_HOURS:g} hours ago, so the next one waits.")
        todo = []
    waiting = sum(1 for r in rows if r["status"] == "queued" and r["post_id"] not in state)
    print(f"{len(todo)} post(s) due now, {waiting} not yet posted in total.")

    if DRY_RUN:
        if TOKEN:
            ig_user, username = find_ig_user()
            print(f"Token works. Connected to @{username} ({ig_user}).")
        else:
            print("No IG_TOKEN set, so the connection was not tested.")
        for r in todo:
            print(f"[dry run] would post {r['post_id']} with {len(r['images'].split('|'))} images")
        return 0
    if not todo:
        return 0
    if not TOKEN:
        print("IG_TOKEN is not set yet, so nothing was posted. Add it under Settings, Secrets and variables, Actions.")
        return 0

    ig_user, username = find_ig_user()
    print(f"Posting as @{username} ({ig_user})")
    failed = False
    for r in todo:
        entry = state.setdefault(r["post_id"], {"attempts": 0})
        entry["attempts"] += 1
        try:
            media_id, link = publish(ig_user, r)
            entry.update(media_id=media_id, permalink=link, posted_at=now.isoformat(timespec="seconds"))
            entry.pop("last_error", None)
            print(f"Posted {r['post_id']}: {link}")
        except Exception as e:                      # keep going, record the reason, fail the run
            entry["last_error"] = str(e)[:500]
            failed = True
            print(f"FAILED {r['post_id']}: {e}")
        with open(STATE, "w") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
