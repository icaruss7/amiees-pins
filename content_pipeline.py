#!/usr/bin/env python3
"""Wrap existing finished assets with local review, upload handoff and lineage.

No media generation or network calls. Live queue changes require two approvals
and an explicit local apply command. Nothing in this build calls that command.
"""

import argparse
import copy
import csv
import hashlib
import io
import json
import math
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from content_assets import PipelineError, digest_file, inside, shopify_url, validate_final, inspect_video
from ig_pipeline_media import reel_media, native_audio

HERE = Path(__file__).resolve().parent
STATES = ("needs_content", "ready_for_review", "approved", "uploaded", "scheduled", "published", "measured")
BOARDS = ("Bee & Animal Jewelry", "Botanical Jewelry", "Celestial Jewelry")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
TEXT_FIELDS = ("content_angle", "hook", "hypothesis", "objective")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def load_json(path):
    if path.stat().st_size > 4 * 1024 * 1024:
        raise PipelineError(f"Metadata file too large: {path.name}")
    def invalid(_):
        raise PipelineError("Non-finite JSON numbers are not supported")
    return json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=invalid)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                     suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(raw)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def iso(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise PipelineError("Use an ISO date/time with an explicit UTC offset") from exc
    if parsed.tzinfo is None:
        raise PipelineError("Scheduled times and receipts need an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def queue_time(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError) as exc:
        raise PipelineError("Queue contains an invalid UTC release_at") from exc


def item_id(manifest):
    return manifest["experiment_id"] + "--" + manifest["platform"]


def queue_id(manifest):
    return ("ig-exp-" if manifest["platform"] == "instagram" else "amiees-exp-") + manifest["experiment_id"]


def read_queue(path):
    raw = path.read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise PipelineError("Queue has missing or duplicate column names")
    rows = list(reader)
    if any(None in row or any(v is None for v in row.values()) for row in rows):
        raise PipelineError("Queue has an incomplete row")
    return raw, reader.fieldnames, rows


def validate_manifest(value, complete=True):
    if not isinstance(value, dict):
        raise PipelineError("manifest.json must be an object")
    schema = load_json(HERE / "content/manifest.schema.json")
    if set(value) - set(schema["properties"]):
        raise PipelineError("Unknown manifest fields; use the documented schema")
    if not set(schema["required"]) <= set(value) or value.get("schema_version") != 1:
        raise PipelineError("Manifest is missing required fields or has an unsupported schema version")
    for name in ("experiment_id", "product"):
        if not isinstance(value[name], str) or not SLUG.fullmatch(value[name]) or len(value[name]) > 100:
            raise PipelineError(f"{name} must be a lowercase hyphenated identifier, at most 100 characters")
    if value["platform"] not in ("instagram", "pinterest"):
        raise PipelineError("platform must be instagram or pinterest")
    formats = ("IMAGE", "CAROUSEL", "REELS") if value["platform"] == "instagram" else ("IMAGE", "VIDEO")
    if value["format"] not in formats:
        raise PipelineError("Unsupported format for this platform")
    for name in TEXT_FIELDS:
        if not isinstance(value[name], str) or (complete and not value[name].strip()):
            raise PipelineError(f"Fill in {name}")
    metric = value["primary_success_metric"]
    if not isinstance(metric, dict) or not isinstance(metric.get("name"), str) or not metric["name"].strip():
        raise PipelineError("primary_success_metric needs a name")
    if value.get("hook_placement") not in ("caption_opening", "overlay"):
        raise PipelineError("hook_placement must be caption_opening or overlay")
    if not isinstance(value["caption_file"], str) or not value["caption_file"]:
        raise PipelineError("caption_file must be a package-relative path")
    pin = value["pinterest"]
    if (not isinstance(pin, dict) or set(pin) != {"board", "title", "description", "destination_url", "keywords"}
            or not all(isinstance(pin[k], str) for k in ("board", "title", "description", "destination_url"))
            or not isinstance(pin["keywords"], list) or not all(isinstance(k, str) for k in pin["keywords"])):
        raise PipelineError("pinterest must contain board, title, description, destination_url and keyword strings")
    if not isinstance(value["source_assets"], list) or not isinstance(value["final_assets"], list):
        raise PipelineError("source_assets and final_assets must be lists")
    if complete and (not value["source_assets"] or not value["final_assets"]):
        raise PipelineError("Declare source assets and final assets")
    paths = []
    for path in value["source_assets"]:
        if not isinstance(path, str):
            raise PipelineError("source_assets contains a non-path entry")
        paths.append(path)
    for asset in value["final_assets"]:
        if not isinstance(asset, dict) or set(asset) != {"path", "role", "alt"}:
            raise PipelineError("Each final asset needs only path, role and alt")
        if asset["role"] not in ("image", "cover", "video") or not isinstance(asset["alt"], str):
            raise PipelineError("Invalid final asset role or alt text")
        if complete and not asset["alt"].strip():
            raise PipelineError("Describe the final asset in alt")
        paths.append(asset["path"])
    if len(paths) != len(set(paths)):
        raise PipelineError("Declare each source/final path only once")
    if complete:
        roles = [a["role"] for a in value["final_assets"]]
        if value["format"] == "IMAGE" and roles != ["image"]:
            raise PipelineError("IMAGE needs exactly one image")
        if value["format"] == "CAROUSEL" and (not 2 <= len(roles) <= 10 or set(roles) != {"image"}):
            raise PipelineError("CAROUSEL needs 2 to 10 ordered images")
        if value["format"] in ("REELS", "VIDEO") and sorted(roles) != ["cover", "video"]:
            raise PipelineError("Video packages need exactly one video and one cover")
    if value.get("scheduled_at") is not None:
        iso(value["scheduled_at"])
    if value.get("planner_reference") is not None and not isinstance(value["planner_reference"], dict):
        raise PipelineError("planner_reference must be an object or null")
    if "is_ai_generated" in value and type(value["is_ai_generated"]) is not bool:
        raise PipelineError("is_ai_generated must be a boolean")
    if value.get("schedule_mode") not in (None, "additional_reel"):
        raise PipelineError("Unknown scheduling mode")
    if value.get("schedule_mode") and (value["platform"] != "instagram" or value["format"] != "REELS"):
        raise PipelineError("Additional Reel slots require an Instagram Reel")
    try:
        native_audio(value)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc
    if "\u2014" in json.dumps(value, ensure_ascii=False):
        raise PipelineError("Remove em dashes from the content manifest")
    return value


class Pipeline:
    def __init__(self, repo=HERE, ffprobe=None, now=None):
        self.repo = Path(repo).resolve()
        self.registry_path = self.repo / "content/experiments.json"
        self.ffprobe = ffprobe
        self.clock = now or (lambda: datetime.now(timezone.utc))

    def now(self):
        return self.clock().astimezone(timezone.utc)

    def event(self, record, action, **details):
        record.setdefault("history", []).append({"at": self.now().isoformat(), "action": action, **details})

    @contextmanager
    def update(self):
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.registry_path.parent / ".pipeline.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise PipelineError("Pipeline is busy or a prior process stopped. Inspect content/.pipeline.lock before retrying.") from exc
        try:
            os.write(fd, f"pid={os.getpid()}\n".encode())
            os.close(fd)
            data = load_json(self.registry_path) if self.registry_path.exists() else {"schema_version": 1, "items": {}}
            if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("items"), dict):
                raise PipelineError("Invalid experiment registry")
            before = copy.deepcopy(data)
            yield data
            if data != before:
                write_json(self.registry_path, data)
        finally:
            lock.unlink(missing_ok=True)

    def known_products(self):
        known = {row["product"] for row in read_queue(self.repo / "ig_queue.csv")[2] if row.get("product")}
        if self.registry_path.exists():
            known.update(load_json(self.registry_path).get("verified_products", {}))
        return known

    def register_product(self, product, url, by):
        reviewer(by)
        if not isinstance(product, str) or not SLUG.fullmatch(product) or len(product) > 100:
            raise PipelineError("Verify a lowercase Shopify product handle first")
        destination(url, product, "verification")
        with self.update() as data:
            data.setdefault("verified_products", {})[product] = {"url": url, "verified_by": by, "verified_at": self.now().isoformat()}
        return data["verified_products"][product]

    def init(self, experiment_id=None, product=None, platform="instagram", format="IMAGE", from_plan=None, folder=None):
        recommendation, reference = {}, None
        if from_plan:
            plan_path = self.repo / "ig_content_plan.json"
            plan = load_json(plan_path)
            recommendation = next((r for r in plan.get("recommendations", []) if r["id"] == from_plan), None)
            if recommendation is None:
                raise PipelineError("Recommendation ID was not found in the current plan")
            experiment_id = experiment_id or recommendation["id"]
            product = recommendation["product"]
            format = recommendation["format"] if platform == "instagram" else (
                "VIDEO" if recommendation["format"] == "REELS" else "IMAGE")
            reference = {"recommendation_id": from_plan, "plan_sha256": digest_file(plan_path)[0],
                         "start_date": plan["start_date"], "test_type": recommendation.get("test_type"),
                         "control": recommendation.get("control"), "platform_adaptation_required": platform != "instagram"}
        manifest = {
            "schema_version": 1, "experiment_id": experiment_id, "product": product,
            "platform": platform, "format": format, "planner_reference": reference,
            **{key: recommendation.get(key, "") for key in TEXT_FIELDS},
            "primary_success_metric": recommendation.get("primary_success_metric", {"name": "reach", "definition": "Accounts reached"}),
            "hook_placement": "caption_opening", "source_assets": ["source/reference.jpg"],
            "final_assets": [], "caption_file": "caption.txt", "scheduled_at": None,
            "pinterest": {"board": "", "title": "", "description": "", "destination_url": "", "keywords": []},
        }
        validate_manifest(manifest, complete=False)
        if product not in self.known_products():
            raise PipelineError("Product is not in the existing catalog; verify its handle and use register-product first")
        if format in ("REELS", "VIDEO"):
            manifest["final_assets"] = [{"path": "final/video.mp4", "role": "video", "alt": ""},
                                         {"path": "final/cover.jpg", "role": "cover", "alt": ""}]
        else:
            manifest["final_assets"] = [{"path": f"final/{i:02d}.jpg", "role": "image", "alt": ""}
                                         for i in range(1, 3 if format == "CAROUSEL" else 2)]
        package = Path(folder).resolve() if folder else self.repo / "content/finished" / item_id(manifest)
        if (package / "manifest.json").exists():
            raise PipelineError("Package already exists; no files were replaced")
        package.mkdir(parents=True, exist_ok=True)
        (package / "source").mkdir(exist_ok=True)
        (package / "final").mkdir(exist_ok=True)
        write_json(package / "manifest.json", manifest)
        self.ingest(package)
        return package

    def inspect(self, package):
        package = Path(package).resolve()
        manifest = validate_manifest(load_json(inside(package, "manifest.json")), complete=False)
        errors, sources, assets, caption = [], [], [], ""
        try:
            validate_manifest(manifest)
            if manifest["product"] not in self.known_products():
                    raise PipelineError("Product handle is not in the existing queue/catalog; verify it with register-product")
            if not inside(package, "READY").is_file():
                raise PipelineError("Write READY last, after all exports and metadata are complete")
            caption_path = inside(package, manifest["caption_file"])
            if caption_path.stat().st_size > 20000:
                raise PipelineError("Caption file is too large")
            caption = caption_path.read_text(encoding="utf-8-sig").strip()
            if not caption or len(caption) > 2200 or "\u2014" in caption:
                raise PipelineError("Use a non-empty caption of at most 2200 characters without em dashes")
            if manifest["hook_placement"] == "caption_opening" and caption.splitlines()[0].strip() != manifest["hook"].strip():
                raise PipelineError("Caption opening must match the declared hook, or set hook_placement to overlay")
            for relative in manifest["source_assets"]:
                sha, size = digest_file(inside(package, relative))
                sources.append({"path": relative, "sha256": sha, "bytes": size})
            assets = [validate_final(package, asset, manifest["platform"], self.ffprobe) for asset in manifest["final_assets"]]
            if manifest["platform"] == "pinterest":
                pin = manifest["pinterest"]
                if (not isinstance(pin, dict) or pin.get("board") not in BOARDS
                        or not 1 <= len(pin.get("title", "")) <= 100
                        or not 1 <= len(pin.get("description", "")) <= 500
                        or not isinstance(pin.get("keywords"), list)):
                    raise PipelineError("Fill a supported Pinterest board, title <=100, description <=500 and keyword list")
                destination(pin["destination_url"], manifest["product"], manifest["experiment_id"])
        except (PipelineError, OSError, UnicodeError, KeyError, TypeError) as exc:
            errors.append(str(exc))
        payload = {"manifest": manifest, "caption": caption, "source_assets": sources, "final_assets": assets}
        return manifest, payload, fingerprint(payload) if not errors else None, errors

    def ingest(self, package):
        manifest, payload, identity, errors = self.inspect(package)
        key = item_id(manifest)
        with self.update() as data:
            for other in data["items"].values():
                if other["experiment_id"] == manifest["experiment_id"] and other["product"] != manifest["product"]:
                    raise PipelineError("An experiment ID cannot refer to different products")
            old = data["items"].get(key)
            if old and old.get("content_sha256") == identity and old.get("validation_errors") == errors:
                return copy.deepcopy(old)
            if old and old["publishing_status"] in ("scheduled", "published", "measured"):
                raise PipelineError("Already scheduled content is immutable; use a new experiment ID for revised assets")
            record = {**copy.deepcopy(manifest), "pipeline_item_id": key, "queue_id": queue_id(manifest),
                      "publishing_status": "needs_content" if errors else "ready_for_review",
                      "content_sha256": identity, "source_assets": payload["source_assets"],
                      "final_assets": payload["final_assets"], "caption": payload["caption"],
                      "validation_errors": errors, "approval": None, "shopify_files": [],
                      "shopify_cdn_urls": [], "scheduled_at": None, "queue_draft": None,
                      "requested_scheduled_at": manifest.get("scheduled_at"),
                      "schedule_approval": None, "published_post_id": None, "permalink": None,
                      "analytics_linkage": None, "history": copy.deepcopy(old.get("history", [])) if old else [],
                      "revisions": copy.deepcopy(old.get("revisions", [])) if old else []}
            if old:
                record["revisions"].append({k: copy.deepcopy(v) for k, v in old.items() if k not in ("history", "revisions")})
            self.event(record, "content_ingested", status=record["publishing_status"], approvals_invalidated=bool(old))
            data["items"][key] = record
            return copy.deepcopy(record)

    def current(self, package, data, states):
        manifest, _, identity, errors = self.inspect(package)
        record = data["items"].get(item_id(manifest))
        if errors or not record or record.get("content_sha256") != identity:
            raise PipelineError("Content changed or is incomplete. Run scan, review and approve again")
        if record["publishing_status"] not in states:
            raise PipelineError(f"Expected state {', '.join(states)}; current state is {record['publishing_status']}")
        return record

    def approve(self, package, by):
        reviewer(by)
        with self.update() as data:
            record = self.current(package, data, ("ready_for_review", "approved"))
            record["approval"] = {"by": by, "at": self.now().isoformat(), "content_sha256": record["content_sha256"]}
            record["publishing_status"] = "approved"
            self.event(record, "creative_approved", by=by, content_sha256=record["content_sha256"])
        return record

    def upload_request(self, package):
        with self.update() as data:
            record = self.current(package, data, ("approved", "uploaded"))
            request = {"schema_version": 1, "experiment_id": record["experiment_id"], "platform": record["platform"],
                       "content_sha256": record["content_sha256"], "upload_automated": False,
                       "instruction": "Upload these exact files to Amiees Shopify Files with the existing trusted tools. Wait for READY and return actual CDN URLs.",
                       "files": [{"path": a["path"], "sha256": a["sha256"], "role": a["role"], "alt": a["alt"],
                                  "upload_filename": f"exp-{record['experiment_id']}-{record['platform']}-{a['sha256'][:12]}-{Path(a['path']).name}"}
                                 for a in record["final_assets"]]}
            write_json(Path(package) / "_pipeline/upload-request.json", request)
        return request

    def record_upload(self, package, receipt):
        receipt = load_json(Path(receipt)) if not isinstance(receipt, dict) else receipt
        if not isinstance(receipt, dict) or not isinstance(receipt.get("files"), list) or not all(isinstance(a, dict) for a in receipt["files"]):
            raise PipelineError("Upload receipt must be an object with a files list")
        with self.update() as data:
            record = self.current(package, data, ("approved", "uploaded"))
            reviewer(receipt.get("verified_by"))
            verified = iso(receipt.get("verified_at"))
            if verified > self.now() + timedelta(minutes=5):
                raise PipelineError("Upload verification time is in the future")
            if (receipt.get("experiment_id"), receipt.get("platform"), receipt.get("content_sha256")) != (
                    record["experiment_id"], record["platform"], record["content_sha256"]):
                raise PipelineError("Upload receipt is for a different experiment, platform or approved revision")
            files = receipt.get("files", [])
            expected = {a["path"]: a for a in record["final_assets"]}
            if len(files) != len(expected) or {a.get("path") for a in files} != set(expected):
                raise PipelineError("Receipt must include every final asset exactly once")
            for asset in files:
                original = expected[asset["path"]]
                gid_type = "Video" if original["role"] == "video" else "MediaImage"
                if (asset.get("sha256") != original["sha256"] or asset.get("file_status") != "READY"
                        or not re.fullmatch(r"gid://shopify/" + gid_type + r"/[0-9]+", asset.get("file_id", ""))):
                    raise PipelineError("Receipt needs matching approved hashes, actual Shopify IDs and READY status")
                shopify_url(asset.get("cdn_url"), original["role"])
            record["shopify_files"] = copy.deepcopy(files)
            record["shopify_cdn_urls"] = [next(a["cdn_url"] for a in files if a["path"] == f["path"]) for f in record["final_assets"]]
            record["upload_receipt"] = copy.deepcopy(receipt)
            record["hosted_video_review"] = None
            record["queue_draft"], record["schedule_approval"] = None, None
            record["publishing_status"] = "uploaded"
            self.event(record, "shopify_upload_recorded", verified_by=receipt["verified_by"])
        return record

    def review_hosted_video(self, package, relative, by):
        """Attest a downloaded, watched CDN rendition. No download or upload here."""
        reviewer(by)
        with self.update() as data:
            record = self.current(package, data, ("uploaded",))
            if record["platform"] != "instagram" or record["format"] != "REELS":
                raise PipelineError("Hosted video review currently supports Instagram Reels only")
            path = inside(Path(package), relative)
            sha, size = digest_file(path)
            metadata = inspect_video(path, self.ffprobe)
            video = next(a for a in record["final_assets"] if a["role"] == "video")
            uploaded = next(a for a in record["shopify_files"] if a["path"] == video["path"])
            record["hosted_video_review"] = {"by": by, "at": self.now().isoformat(),
                "content_sha256": record["content_sha256"],
                "upload_receipt_sha256": fingerprint(record["upload_receipt"]),
                "cdn_url": uploaded["cdn_url"], "reviewed_file": relative,
                "sha256": sha, "bytes": size, "metadata": metadata}
            record["queue_draft"], record["schedule_approval"] = None, None
            self.event(record, "hosted_video_reviewed", by=by, sha256=sha)
        return record

    def queue_path(self, platform):
        return self.repo / ("ig_queue.csv" if platform == "instagram" else "pins.csv")

    def check_time(self, platform, rows, when, identity, additional=False):
        if when < self.now() + timedelta(hours=1):
            raise PipelineError("Choose a slot at least one hour in the future; this command cannot queue an immediate post")
        state_path = self.repo / "ig_state.json"
        state = load_json(state_path) if state_path.exists() else {}
        registry = load_json(self.registry_path) if self.registry_path.exists() else {}
        extra_ids = {r.get("queue_id") for r in registry.get("items", {}).values()
                     if r.get("schedule_mode") == "additional_reel"}
        for row in rows:
            key = row.get("post_id") or row.get("guid")
            if key == identity or row.get("status") != "queued":
                continue
            if platform == "instagram" and any(state.get(key, {}).get(k) for k in ("media_id", "permalink", "posted_at")):
                continue
            scheduled = queue_time(row["release_at"])
            extra_pair = platform == "instagram" and (additional or key in extra_ids)
            if platform == "instagram" and scheduled < self.now():
                # Extra future tests may coexist with a routine due row, never an uncertain publication.
                if (not additional or when < self.now() + timedelta(hours=12)
                        or state.get(key, {}).get("phase") in ("publishing", "needs_review")
                        or state.get(key, {}).get("last_error")):
                    raise PipelineError("Resolve overdue unpublished Instagram rows before adding another post")
            gap = timedelta(hours=8 if extra_pair else 20) if platform == "instagram" else timedelta(0)
            if abs(when - scheduled) < gap or when == scheduled:
                raise PipelineError("Requested slot conflicts with an existing queue entry. Do not move the Nature Series")

    def prepare_queue(self, package, when):
        when = iso(when)
        with self.update() as data:
            record = self.current(package, data, ("uploaded",))
            if record.get("requested_scheduled_at") and iso(record["requested_scheduled_at"]) != when:
                raise PipelineError("Requested time differs from the reviewed manifest schedule")
            is_reel = record["platform"] == "instagram" and record["format"] == "REELS"
            if record["format"] not in ("IMAGE", "CAROUSEL") and not is_reel:
                raise PipelineError("Pinterest video is blocked by the image-only RSS publisher")
            try:
                media = reel_media(record) if is_reel else None
            except ValueError as exc:
                raise PipelineError(str(exc)) from exc
            path = self.queue_path(record["platform"])
            raw, headers, rows = read_queue(path)
            self.check_time(record["platform"], rows, when, record["queue_id"], record.get("schedule_mode") == "additional_reel")
            release = when.strftime("%Y-%m-%d %H:%M:%S")
            if record["platform"] == "instagram":
                row = dict(post_id=record["queue_id"], release_at=release, product=record["product"],
                           caption=record["caption"], images="" if is_reel else "|".join(record["shopify_cdn_urls"]), status="queued")
            else:
                pin = record["pinterest"]
                if any(r["title"].strip().casefold() == pin["title"].strip().casefold() for r in rows):
                    raise PipelineError("Pinterest title already exists; review a distinct title")
                row = dict(guid=record["queue_id"], release_at=release, board=pin["board"], title=pin["title"],
                           description=pin["description"], image=record["shopify_cdn_urls"][0],
                           link=destination(pin["destination_url"], record["product"], record["experiment_id"]),
                           keywords=", ".join(pin["keywords"]), status="queued")
            if set(row) != set(headers):
                raise PipelineError("Queue columns changed. Review publisher compatibility before preparing a row")
            if any((r.get("post_id") or r.get("guid")) == record["queue_id"] for r in rows):
                raise PipelineError("This experiment already has a queue ID; sync instead of adding it again")
            draft = {"schema_version": 1, "pipeline_item_id": record["pipeline_item_id"],
                     "experiment_id": record["experiment_id"], "content_sha256": record["content_sha256"],
                     "upload_receipt_sha256": fingerprint(record["upload_receipt"]),
                     "queue_file": path.name, "queue_base_sha256": hashlib.sha256(raw).hexdigest(),
                     "row": row, "scheduled_at": when.isoformat(), "auto_apply": False}
            if media:
                draft["media"] = media
            record["queue_draft"], record["schedule_approval"] = draft, None
            write_json(Path(package) / "_pipeline/queue-draft.json", draft)
            self.event(record, "queue_draft_prepared", scheduled_at=draft["scheduled_at"])
        return draft

    def approve_schedule(self, package, by):
        reviewer(by)
        with self.update() as data:
            record = self.current(package, data, ("uploaded",))
            draft = record.get("queue_draft")
            if not draft or load_json(Path(package) / "_pipeline/queue-draft.json") != draft:
                raise PipelineError("Prepare and review the current queue draft first")
            raw, _, rows = read_queue(self.queue_path(record["platform"]))
            if hashlib.sha256(raw).hexdigest() != draft["queue_base_sha256"]:
                raise PipelineError("Queue changed since the draft was prepared; prepare it again")
            self.check_time(record["platform"], rows,
                            iso(draft["scheduled_at"]), record["queue_id"], record.get("schedule_mode") == "additional_reel")
            record["schedule_approval"] = {"by": by, "at": self.now().isoformat(), "draft_sha256": fingerprint(draft)}
            self.event(record, "queue_entry_approved", by=by, draft_sha256=fingerprint(draft))
        return record

    def apply_queue(self, package, confirm=False):
        if not confirm:
            raise PipelineError("Explicit --confirm-add-to-live-queue is required after schedule approval")
        with self.update() as data:
            record = self.current(package, data, ("uploaded", "scheduled"))
            draft, approval = record.get("queue_draft"), record.get("schedule_approval")
            if not draft or not approval or approval["draft_sha256"] != fingerprint(draft):
                raise PipelineError("The exact queue draft needs separate scheduling approval")
            if (load_json(Path(package) / "_pipeline/queue-draft.json") != draft
                    or draft["content_sha256"] != record["content_sha256"]
                    or draft["upload_receipt_sha256"] != fingerprint(record["upload_receipt"])):
                raise PipelineError("Approved draft or uploaded assets changed; review again")
            path = self.queue_path(record["platform"])
            raw, headers, rows = read_queue(path)
            matches = [r for r in rows if (r.get("post_id") or r.get("guid")) == record["queue_id"]]
            if matches:
                if len(matches) != 1 or matches[0] != draft["row"]:
                    raise PipelineError("Existing queue ID has different content; nothing was overwritten")
            else:
                if hashlib.sha256(raw).hexdigest() != draft["queue_base_sha256"]:
                    raise PipelineError("Queue changed since review. Prepare and approve a fresh draft")
                self.check_time(record["platform"], rows, iso(draft["scheduled_at"]), record["queue_id"], record.get("schedule_mode") == "additional_reel")
                newline = "\r\n" if b"\r\n" in raw else "\n"
                stream = io.StringIO(newline="")
                writer = csv.DictWriter(stream, fieldnames=headers, lineterminator=newline)
                writer.writerow(draft["row"])
                addition = (b"" if not raw or raw.endswith(b"\n") else newline.encode()) + stream.getvalue().encode("utf-8")
                # Never rewrite a header, existing row, schedule, GUID or original byte.
                with path.open("ab") as target:
                    target.write(addition)
                    target.flush()
                    os.fsync(target.fileno())
                if not path.read_bytes().startswith(raw):
                    raise PipelineError("Concurrent queue edit detected. Reconcile before committing")
            if record["publishing_status"] != "scheduled":
                record["publishing_status"] = "scheduled"
                record["scheduled_at"] = draft["scheduled_at"]
                record["queue_confirmation"] = "Local queue row only. Commit and push registry plus queue for GitHub scheduling."
                self.event(record, "local_queue_row_added", by=approval["by"])
        return record

    def sync(self):
        state = load_json(self.repo / "ig_state.json") if (self.repo / "ig_state.json").exists() else {}
        analytics = load_json(self.repo / "ig_analytics.json") if (self.repo / "ig_analytics.json").exists() else {}
        queues = {platform: read_queue(self.queue_path(platform))[2] for platform in ("instagram", "pinterest")}
        with self.update() as data:
            for record in data["items"].values():
                if record["publishing_status"] not in ("uploaded", "scheduled", "published", "measured"):
                    continue
                rows = [r for r in queues[record["platform"]] if (r.get("post_id") or r.get("guid")) == record["queue_id"]]
                draft = record.get("queue_draft")
                if not draft or not record.get("schedule_approval"):
                    continue
                if not rows or rows != [draft["row"]]:
                    record["queue_observation"] = ("awaiting_explicit_apply" if record["publishing_status"] == "uploaded"
                                                   else "missing_or_changed; reconcile before drawing conclusions")
                    continue
                record["queue_observation"] = "exact_approved_row_present"
                if record["publishing_status"] == "uploaded":
                    record["publishing_status"] = "scheduled"
                    record["scheduled_at"] = draft["scheduled_at"]
                    self.event(record, "queue_row_observed")
                if record["platform"] == "pinterest":
                    record["feed_release_eligible"] = queue_time(rows[0]["release_at"]) <= self.now()
                    continue  # Feed eligibility is not proof of a public Pinterest Pin.
                published = state.get(record["queue_id"], {})
                if not published.get("media_id") or not published.get("posted_at"):
                    continue
                published_at = iso(published["posted_at"])
                if published_at > self.now():
                    continue
                record["published_post_id"] = published["media_id"]
                record["permalink"] = published.get("permalink")
                if record["publishing_status"] == "scheduled":
                    record["publishing_status"] = "published"
                    record["published_at"] = published_at.isoformat()
                    self.event(record, "publication_observed")
                observed = analytics.get("posts", {}).get(record["queue_id"], {})
                counts = observed.get("latest", {})
                snapshots = [h for h in observed.get("history", []) if h.get("collected_at")]
                if observed.get("media_id") != record["published_post_id"] or not snapshots:
                    continue
                collected = max(iso(h["collected_at"]) for h in snapshots)
                if collected < published_at or collected > self.now() or not any(
                        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 for v in counts.values()):
                    continue
                record["analytics_linkage"] = {"file": "ig_analytics.json", "post_id": record["queue_id"],
                                               "experiment_id": record["experiment_id"], "media_id": observed["media_id"],
                                               "collected_at": collected.isoformat(), "latest": copy.deepcopy(counts)}
                if record["publishing_status"] != "measured":
                    record["publishing_status"] = "measured"
                    self.event(record, "analytics_linked")
        return data

    def record_pin_result(self, key, receipt):
        receipt = load_json(Path(receipt))
        if not isinstance(receipt, dict):
            raise PipelineError("Pinterest result receipt must be an object")
        reviewer(receipt.get("verified_by"))
        observed_at = iso(receipt.get("observed_at"))
        if observed_at > self.now():
            raise PipelineError("Observation time is in the future")
        with self.update() as data:
            record = data["items"].get(key)
            if not record or record["platform"] != "pinterest" or record["publishing_status"] not in ("scheduled", "published", "measured"):
                raise PipelineError("A scheduled Pinterest item is required")
            pin_id = str(receipt.get("published_post_id", ""))
            if (receipt.get("experiment_id") != record["experiment_id"] or not pin_id.isdigit()
                    or receipt.get("permalink") != f"https://www.pinterest.com/pin/{pin_id}/"):
                raise PipelineError("Supply the verified public Pinterest Pin ID/link for this experiment")
            if record.get("published_post_id") and record["published_post_id"] != pin_id:
                raise PipelineError("Published Pin identity cannot change")
            record.update(published_post_id=pin_id, permalink=receipt["permalink"],
                          publishing_status="measured" if record["publishing_status"] == "measured" else "published",
                          pinterest_result=receipt)
            self.event(record, "pinterest_publication_attested", by=receipt["verified_by"])
            if "metrics" in receipt:
                metrics = receipt["metrics"]
                if not isinstance(metrics, dict) or not metrics or not all(
                    v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0)
                    for v in metrics.values()) or not any(v is not None for v in metrics.values()):
                    raise PipelineError("Pinterest metrics need at least one measured count; unknown values stay null")
                record["publishing_status"] = "measured"
                record["analytics_linkage"] = {"source": "manual_pinterest_receipt", "experiment_id": record["experiment_id"],
                                               "collected_at": observed_at.isoformat(), "latest": metrics}
        return record

    def scan(self, directory):
        results = []
        directory = Path(directory).resolve()
        for manifest in sorted(directory.glob("*/manifest.json")):
            try:
                if not manifest.parent.resolve().is_relative_to(directory):
                    raise PipelineError("Package symlink escapes the watched folder")
                record = self.ingest(manifest.parent)
                results.append({"package": manifest.parent.name, "status": record["publishing_status"], "errors": record["validation_errors"]})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                results.append({"package": manifest.parent.name, "status": "error", "errors": [str(exc)]})
        return results


def destination(url, product, experiment):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "amiees.com" or parsed.username or parsed.password
            or parsed.port is not None or parsed.fragment or parsed.path.rstrip("/") != f"/products/{product}"):
        raise PipelineError("Pinterest destination must be this product's exact HTTPS amiees.com page")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(utm_source="pinterest", utm_medium="organic", utm_campaign="content_experiments", utm_content=experiment)
    return urlunsplit(("https", "amiees.com", parsed.path, urlencode(query), ""))


def reviewer(value):
    if not isinstance(value, str) or not value.strip():
        raise PipelineError("Name the person who actually reviewed this exact revision")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=HERE)
    parser.add_argument("--ffprobe", help="Existing ffprobe executable, if not on PATH")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--experiment-id")
    init.add_argument("--product")
    init.add_argument("--platform", choices=("instagram", "pinterest"), default="instagram")
    init.add_argument("--format", default="IMAGE")
    init.add_argument("--from-plan")
    init.add_argument("--folder", type=Path)
    for name in ("scan", "watch"):
        command = commands.add_parser(name)
        command.add_argument("--finished-dir", type=Path)
        if name == "watch":
            command.add_argument("--interval", type=int, default=30)
    for name in ("ingest", "approve", "upload-request", "record-upload", "review-hosted-video", "prepare-queue", "approve-schedule", "apply-queue"):
        command = commands.add_parser(name)
        command.add_argument("--package", type=Path, required=True)
        if name in ("approve", "approve-schedule", "review-hosted-video"):
            command.add_argument("--reviewed-by", required=True)
        if name == "record-upload":
            command.add_argument("--receipt", type=Path, required=True)
        if name == "review-hosted-video":
            command.add_argument("--file", required=True, help="Package-relative downloaded Shopify rendition, watched by reviewer")
        if name == "prepare-queue":
            command.add_argument("--at", required=True)
        if name == "apply-queue":
            command.add_argument("--confirm-add-to-live-queue", action="store_true")
    commands.add_parser("sync")
    register = commands.add_parser("register-product")
    register.add_argument("--product", required=True)
    register.add_argument("--url", required=True)
    register.add_argument("--verified-by", required=True)
    pin = commands.add_parser("record-pin-result")
    pin.add_argument("--item-id", required=True)
    pin.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    pipeline = Pipeline(args.repo, args.ffprobe)
    try:
        if args.command == "init":
            print(pipeline.init(args.experiment_id, args.product, args.platform, args.format, args.from_plan, args.folder))
            return 0
        if args.command in ("scan", "watch"):
            directory = args.finished_dir or args.repo / "content/finished"
            if args.command == "watch" and not 1 <= args.interval <= 60:
                raise PipelineError("Watch interval must be 1 to 60 seconds")
            while True:
                results = pipeline.scan(directory)
                print(json.dumps(results, indent=2), flush=True)
                if args.command == "scan":
                    return 1 if any(r["status"] == "error" for r in results) else 0
                time.sleep(args.interval)
        calls = {
            "ingest": lambda: pipeline.ingest(args.package),
            "approve": lambda: pipeline.approve(args.package, args.reviewed_by),
            "upload-request": lambda: pipeline.upload_request(args.package),
            "record-upload": lambda: pipeline.record_upload(args.package, args.receipt),
            "review-hosted-video": lambda: pipeline.review_hosted_video(args.package, args.file, args.reviewed_by),
            "prepare-queue": lambda: pipeline.prepare_queue(args.package, args.at),
            "approve-schedule": lambda: pipeline.approve_schedule(args.package, args.reviewed_by),
            "apply-queue": lambda: pipeline.apply_queue(args.package, args.confirm_add_to_live_queue),
            "sync": pipeline.sync,
            "record-pin-result": lambda: pipeline.record_pin_result(args.item_id, args.receipt),
            "register-product": lambda: pipeline.register_product(args.product, args.url, args.verified_by),
        }
        result = calls[args.command]()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Content pipeline stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
