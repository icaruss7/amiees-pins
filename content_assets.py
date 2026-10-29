"""Local validation only. No generation, upload, transcoding or network calls."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit


class PipelineError(ValueError):
    pass


def inside(package, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative or Path(relative).is_absolute():
        raise PipelineError("Asset paths must be package-relative paths using forward slashes")
    if ":" in relative or ".." in Path(relative).parts:
        raise PipelineError("Asset paths cannot contain drive letters or traversal")
    result = (package / relative).resolve()
    if not result.is_relative_to(package.resolve()):
        raise PipelineError("Asset or symlink escapes the content package")
    return result


def digest_file(path, max_bytes=100 * 1024 * 1024):
    before = path.stat()
    if not path.is_file() or before.st_size == 0 or before.st_size > max_bytes:
        raise PipelineError(f"Missing, empty or oversized asset: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise PipelineError(f"Asset is still changing: {path.name}")
    return digest.hexdigest(), after.st_size


def inspect_image(path):
    try:
        from PIL import Image
    except ImportError as exc:
        raise PipelineError("Image validation needs: python -m pip install -r requirements-content-pipeline.txt") from exc
    try:
        with Image.open(path) as image:
            kind, size = image.format, image.size
            if not 1 <= size[0] <= 8192 or not 1 <= size[1] <= 8192 or size[0] * size[1] > 40000000:
                raise PipelineError("Image dimensions exceed the ingestion safety limit")
            image.verify()
        with Image.open(path) as image:
            image.load()
    except Exception as exc:
        raise PipelineError(f"Image cannot be decoded: {path.name}") from exc
    return {"media_type": kind, "width": size[0], "height": size[1]}


def inspect_video(path, ffprobe=None):
    with path.open("rb") as stream:
        if b"ftyp" not in stream.read(32):
            raise PipelineError("Video must be an actual MP4 file")
    binary = ffprobe or os.getenv("AMIEES_FFPROBE") or shutil.which("ffprobe")
    if not binary:
        raise PipelineError("Video validation needs ffprobe on PATH or AMIEES_FFPROBE set to its existing executable")
    try:
        result = subprocess.run([str(binary), "-v", "error", "-protocol_whitelist", "file,pipe",
                                 "-show_streams", "-show_format", "-of", "json", str(path)],
                                capture_output=True, text=True, timeout=30, check=True)
        report = json.loads(result.stdout)
        video = next(s for s in report["streams"] if s.get("codec_type") == "video")
        audio = [s for s in report["streams"] if s.get("codec_type") == "audio"]
        info = {"media_type": "MP4", "codec": video["codec_name"],
                "width": int(video["width"]), "height": int(video["height"]),
                "duration_seconds": float(report["format"]["duration"]),
                "audio_codecs": [s["codec_name"] for s in audio]}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration) as exc:
        raise PipelineError(f"Video cannot be validated: {path.name}") from exc
    if info["codec"] != "h264" or not 3 <= info["duration_seconds"] <= 180:
        raise PipelineError("Use a 3 to 180 second H.264 MP4 for this pipeline")
    if info["height"] <= 0 or info["width"] < 720 or abs(info["width"] / info["height"] - 9 / 16) > 0.03:
        raise PipelineError("Use a portrait 9:16 video at 720 pixels wide or higher")
    if any(codec != "aac" for codec in info["audio_codecs"]):
        raise PipelineError("MP4 audio must be AAC; a silent approved master is also accepted")
    return info


def validate_final(package, asset, platform, ffprobe=None):
    path = inside(package, asset["path"])
    sha, size = digest_file(path, 100 * 1024 * 1024 if asset["role"] == "video" else 8 * 1024 * 1024)
    if asset["role"] == "video":
        if path.suffix.lower() != ".mp4":
            raise PipelineError("Final video filename must end in .mp4")
        info = inspect_video(path, ffprobe)
    else:
        if path.suffix.lower() not in (".jpg", ".jpeg"):
            raise PipelineError("Final image/cover files must be JPEG; keep source PNGs in source/")
        info = inspect_image(path)
        if info["media_type"] != "JPEG":
            raise PipelineError("A JPEG filename must contain JPEG bytes")
        ratio = info["width"] / info["height"]
        if asset["role"] == "cover":
            if info["width"] < 720 or abs(ratio - 9 / 16) > 0.03:
                raise PipelineError("Video covers must be portrait 9:16, at least 720 pixels wide")
        elif platform == "pinterest":
            if info["width"] < 1000 or abs(ratio - 2 / 3) > 0.02:
                raise PipelineError("Pinterest RSS images must be 2:3 JPEGs, at least 1000 pixels wide")
        elif info["width"] < 320 or not 0.8 <= ratio <= 1.91:
            raise PipelineError("Use an Instagram JPEG with width >=320 and aspect ratio 4:5 through 1.91:1")
    return {**asset, "sha256": sha, "bytes": size, **info}


def shopify_url(url, role):
    if not isinstance(url, str):
        raise PipelineError("A Shopify CDN URL is required")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port is not None
            or parsed.fragment or ".." in unquote(parsed.path).split("/")):
        raise PipelineError("Use a stable HTTPS Shopify URL without credentials, port or fragment")
    image_path = (parsed.hostname == "cdn.shopify.com" and
                  parsed.path.startswith("/s/files/1/0677/3201/3371/files/"))
    storefront_path = parsed.hostname == "amiees.com" and parsed.path.startswith("/cdn/shop/files/")
    video_path = parsed.hostname == "cdn.shopify.com" and parsed.path.startswith("/videos/c/o/v/")
    if role == "video":
        if not video_path or not parsed.path.lower().endswith(".mp4"):
            raise PipelineError("Record the processed Shopify MP4 source URL, not a preview or HLS playlist")
    elif not (image_path or storefront_path) or not parsed.path.lower().endswith((".jpg", ".jpeg")):
        raise PipelineError("Record the Amiees Shopify Files JPEG URL")
    return url
