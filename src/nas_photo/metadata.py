import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from .config import relative

MAX_TOOL_OUTPUT = 2 * 1024 * 1024
EXTENSIONS = {"jpg": "JPEG", "jpeg": "JPEG", "jfif": "JPEG", "png": "PNG", "heic": "HEIC",
              "heif": "HEIC", "mov": "MOV", "mp4": "MP4", "webp": "WEBP", "tif": "TIFF", "tiff": "TIFF"}


def secure_path(root, rel):
    root = Path(root)
    rel = relative(rel)
    # Reject symlinks, including internal links, instead of traversing unexpected datasets.
    if root.is_symlink():
        raise ValueError("symlink_source_root")
    real_root = root.resolve(strict=True)
    target = root / rel
    cursor = root
    for part in Path(rel).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("symlink_input")
    if not target.resolve(strict=True).is_relative_to(real_root):
        raise ValueError("path_escape")
    return target


def snapshot(path):
    s = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(s.st_mode):
        raise ValueError("input_not_regular_file")
    fs = {"size": s.st_size, "mtime": str(s.st_mtime), "mtime_ns": str(s.st_mtime_ns),
          "birthtime": str(s.st_birthtime) if hasattr(s, "st_birthtime") else None,
          "ctime": str(s.st_ctime), "ctime_meaning": "status_change_not_creation"}
    return fs, (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)


def fingerprint(fs):
    return json.dumps({k: fs.get(k) for k in ["size", "mtime_ns", "birthtime"]}, sort_keys=True)


def tool(command, timeout):
    # File-backed bounded output avoids loading arbitrarily large tool output into memory.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        p = subprocess.Popen(command, stdout=out, stderr=err)
        try:
            p.wait(timeout=timeout)
        except BaseException:
            p.kill()
            p.wait(timeout=2)
            raise
        if out.tell() > MAX_TOOL_OUTPUT or err.tell() > MAX_TOOL_OUTPUT:
            raise ValueError("tool_output_limit")
        out.seek(0); err.seek(0)
        return p.returncode, out.read().decode("utf-8", "replace"), err.read().decode("utf-8", "replace")


def signature_format(header):
    if header.startswith(b"\xff\xd8\xff"): return "JPEG"
    if header.startswith(b"\x89PNG\r\n\x1a\n"): return "PNG"
    if header[:4] in [b"II*\x00", b"MM\x00*"]: return "TIFF"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP": return "WEBP"
    if header[4:8] == b"ftyp":
        brands = header[8:64]
        if any(b in brands for b in [b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"]): return "HEIC"
        if header[8:12] == b"qt  ": return "MOV"
        return "MP4"
    return None


def canonical(value):
    if value is None: return None
    v = str(value).upper()
    return {"JPG": "JPEG", "IMAGE/JPEG": "JPEG", "IMAGE/PNG": "PNG", "HEIF": "HEIC",
            "IMAGE/HEIC": "HEIC", "IMAGE/HEIF": "HEIC", "VIDEO/QUICKTIME": "MOV",
            "VIDEO/MP4": "MP4", "IMAGE/TIFF": "TIFF", "IMAGE/WEBP": "WEBP"}.get(v, v if v in EXTENSIONS.values() else None)


def extract(request):
    path = secure_path(request["root"], request["relative_path"])
    fs, before = snapshot(path)
    if fs["size"] > request["max_file_bytes"]:
        raise ValueError("file_size_limit")
    fp = fingerprint(fs)
    if fp == request.get("previous_fingerprint") and request.get("previous_extractor") == request["extractor"]:
        return {"unchanged": True, "fs": fs, "fingerprint": fp}
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as f: header = f.read(64)
    votes = [{"tool": "signature", "format": signature_format(header)},
             {"tool": "extension", "format": EXTENSIONS.get(path.suffix.lower()[1:])}]
    rc, mime, warning = tool(["file", "-b", "--mime-type", str(path)], 10)
    votes.append({"tool": "file", "format": canonical(mime.strip()), "raw": mime.strip(), "exit": rc})
    tags = ["FileType", "MIMEType", "ImageWidth", "ImageHeight", "ExifImageWidth", "ExifImageHeight",
            "Orientation", "DateTimeOriginal", "CreateDate", "ModifyDate", "OffsetTime", "OffsetTimeOriginal",
            "OffsetTimeDigitized", "SubSecTimeOriginal", "Make", "Model", "LensModel", "LensID",
            "GPSLatitude", "GPSLongitude", "GPSVersionID", "ContentIdentifier", "MediaGroupUUID",
            "Duration", "Warning", "Error", "ExifToolVersion"]
    command = [request["exiftool"], "-config", "", "-json", "-G1:4", "-a", "-s", "-n",
               "-charset", "filename=UTF8"] + ["-" + t for t in tags] + ["--", str(path)]
    rc, text, stderr = tool(command, request["tool_timeout"])
    data = json.loads(text)
    if not isinstance(data, list) or len(data) != 1:
        raise ValueError("invalid_metadata_response")
    raw = data[0]
    if rc != 0 or any(k.split(":")[-1] == "Error" for k in raw):
        raise ValueError("metadata_tool_failed: " + str(raw.get("ExifTool:Error", stderr))[:300])
    def values(tag):
        return [(k, v) for k, v in raw.items() if k.split(":")[-1] == tag]
    def first(tag):
        vals = values(tag)
        return vals[0][1] if vals else None
    gps = any(k.split(":")[-1].startswith("GPS") for k in raw)
    # Keep selected original values, excluding coordinates and physical access paths.
    raw = {k: v for k, v in raw.items() if k != "SourceFile" and not k.split(":")[-1].startswith("GPS")}
    raw["privacy:GPSFieldsPresent"] = gps
    def clean(v): return v.rstrip("\x00").strip() if isinstance(v, str) else v
    fmt = canonical(first("FileType"))
    votes.append({"tool": "exiftool", "format": fmt, "raw": first("FileType")})
    actual = votes[0]["format"] or fmt or canonical(mime.strip())
    if actual not in EXTENSIONS.values():
        raise ValueError("unsupported_or_unidentified_format")
    kind = "video" if actual in ["MOV", "MP4"] else "image"
    meta = {"format": actual, "kind": kind, "width": first("ImageWidth") or first("ExifImageWidth"),
            "height": first("ImageHeight") or first("ExifImageHeight"), "orientation": first("Orientation"),
            "make": clean(first("Make")), "model": clean(first("Model")),
            "lens": clean(first("LensModel")), "gps_present": gps, "time_fields": []}
    for tag, role, offset_tag in [("DateTimeOriginal", "exif_original", "OffsetTimeOriginal"),
                                   ("CreateDate", "digitized_or_container", "OffsetTimeDigitized"),
                                   ("ModifyDate", "metadata_modify", "OffsetTime")]:
        for key, value in values(tag):
            meta["time_fields"].append({"field": key, "raw": value, "origin": role, "offset": first(offset_tag)})
    video = None
    if kind == "video":
        rc, text, err = tool([request["ffprobe"], "-v", "error", "-probesize", "2097152",
                             "-analyzeduration", "1000000", "-show_entries",
                             "format=format_name,duration:format_tags=creation_time:stream=codec_type,codec_name,width,height:stream_tags=creation_time",
                             "-of", "json", str(path)], request["tool_timeout"])
        if rc: raise ValueError("video_probe_failed: " + err[:300])
        video = json.loads(text)
        for i, stream in enumerate(video.get("streams", [])):
            if stream.get("codec_type") == "video":
                meta["width"], meta["height"] = stream.get("width"), stream.get("height")
            date = stream.get("tags", {}).get("creation_time")
            if date: meta["time_fields"].append({"field": f"ffprobe.stream.{i}.creation_time", "raw": date, "origin": "video_container"})
        date = video.get("format", {}).get("tags", {}).get("creation_time")
        if date: meta["time_fields"].append({"field": "ffprobe.format.creation_time", "raw": date, "origin": "video_container"})
    prior = request.get("prior_evidence", {})
    for name, value in prior.get("format_votes", {}).items():
        votes.append({"tool": name, "format": canonical(value), "historical": True})
    after_fs, after = snapshot(path)
    if before != after or fingerprint(after_fs) != fp:
        raise ValueError("file_changed_during_read")
    return {"unchanged": False, "fs": fs, "fingerprint": fp, "raw": raw, "metadata": meta,
            "format_votes": votes, "video": video, "prior_evidence": prior,
            "tools": {"exiftool": first("ExifToolVersion")}, "size_mtime_unchanged": True}
