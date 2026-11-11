"""Evidence-preserving calendar parsing. Unknown timezone never becomes UTC."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import re


def parse_time(field, raw, origin, offset=None, epoch=False):
    out = {"field": field, "raw": None if raw is None else str(raw), "origin": origin,
           "offset_raw": offset, "parsed": None, "utc": None, "timezone": "unknown",
           "error": None}
    if raw is None or raw == "":
        out["error"] = "missing"
        return out
    try:
        if epoch:
            n = Decimal(str(raw))
            if not n.is_finite():
                raise ValueError("non-finite epoch")
            dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=float(n))
        else:
            text = str(raw).strip().rstrip("\x00")
            text = re.sub(r"^(\d{4}):(\d{2}):(\d{2})", r"\1-\2-\3", text)
            text = text.replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None and offset:
                if not re.fullmatch(r"[+-]\d{2}:\d{2}", str(offset)):
                    raise ValueError("invalid timezone offset")
                hours, minutes = map(int, str(offset)[1:].split(":"))
                if hours > 23 or minutes > 59:
                    raise ValueError("invalid timezone offset")
                delta = timedelta(hours=hours, minutes=minutes)
                dt = dt.replace(tzinfo=timezone(delta if offset[0] == "+" else -delta))
        out["parsed"] = dt.isoformat()
        if dt.tzinfo is not None:
            out["utc"] = dt.astimezone(timezone.utc).isoformat()
            out["timezone"] = "explicit_or_filesystem_utc"
    except (ValueError, TypeError, OverflowError, InvalidOperation, OSError) as e:
        out["error"] = type(e).__name__ + ": " + str(e)[:150]
    return out


def auxiliary_dates(relative_path):
    evidence = []
    name = relative_path.rsplit("/", 1)[-1]
    match = re.search(r"(?<!\d)((?:19|20)\d{2})[-_]?([01]\d)[-_]?([0-3]\d)[_ -]?([0-2]\d)([0-5]\d)([0-5]\d)(?!\d)", name)
    if match:
        y, m, d, h, mi, s = match.groups()
        evidence.append(parse_time("filename_datetime", f"{y}-{m}-{d}T{h}:{mi}:{s}", "filename"))
    # Partial directory dates stay partial, without inventing a day or timezone.
    parts = relative_path.split("/")[:-1]
    for i, part in enumerate(parts):
        if re.fullmatch(r"(?:19|20)\d{2}", part):
            precision = "year"
            value = part
            if i + 1 < len(parts) and parts[i + 1].isdigit() and 1 <= int(parts[i + 1]) <= 12:
                precision = "month"
                value += "-" + parts[i + 1].zfill(2)
            evidence.append({"field": "directory_date", "raw": value, "origin": "directory",
                             "precision": precision, "timezone": "unknown", "parsed": None,
                             "utc": None, "error": None})
    return evidence


def analyze(fs, metadata, votes, rules, relpath, now=None):
    now = now or datetime.now(timezone.utc)
    times = [parse_time(k, fs.get(k), "filesystem", epoch=True)
             for k in ["mtime", "birthtime"] if fs.get(k) is not None]
    fields = metadata.get("time_fields", [])
    times.extend(parse_time(x["field"], x.get("raw"), x["origin"], x.get("offset")) for x in fields)
    times.extend(auxiliary_dates(relpath))
    labels = {}
    def add(tag, reason):
        labels.setdefault(tag, []).append(reason)
    threshold = datetime.fromisoformat(rules["early_filesystem_before"]).date()
    future = now + timedelta(days=rules["future_tolerance_days"])
    for t in times:
        if t["error"] and t["error"] != "missing":
            add("invalid_timestamp", {"field": t["field"], "origin": t["origin"], "reason": t["error"]})
        if not t["parsed"]:
            continue
        dt = datetime.fromisoformat(t["parsed"])
        if t["origin"] == "filesystem" and dt.date() < threshold:
            add("early_filesystem_timestamp", {"field": t["field"], "screening_only": True,
                                                "threshold": str(threshold)})
        # Unknown timezone is compared by calendar day only, never coerced to UTC.
        is_future = dt > future if dt.tzinfo else dt.date() > future.date()
        if is_future:
            add("future_timestamp", {"field": t["field"], "timezone_unknown": dt.tzinfo is None})
    captures = [t for t in times if t["origin"] in ["exif_original", "video_container"] and t["parsed"]]
    if not captures:
        add("missing_capture_time", {"meaning": "no parseable recorded original/container time; no truth inferred"})
    if any(t["timezone"] == "unknown" for t in captures):
        add("timezone_uncertain", {"meaning": "capture candidate lacks explicit offset"})
    aware = [t for t in times if t["utc"] and t["origin"] not in ["directory", "filename"]]
    # Differences are evidence of divergence, not proof that either field is wrong.
    for i, a in enumerate(aware):
        for b in aware[i + 1:]:
            delta = abs((datetime.fromisoformat(a["utc"]) - datetime.fromisoformat(b["utc"])).total_seconds())
            if delta > rules["conflict_tolerance_hours"] * 3600:
                add("timestamp_conflict", {"a": a["field"], "b": b["field"], "difference_seconds": delta,
                                             "may_reflect_copy_or_export": True})
    observed = {v["format"] for v in votes if v.get("format") and v["tool"] != "extension"}
    if len(observed) > 1:
        add("format_identification_conflict", {"observations": votes})
    image = metadata.get("kind") == "image"
    completeness = {"capture_time_recorded": bool(captures), "camera_recorded": bool(metadata.get("model")),
                    "lens_recorded": bool(metadata.get("lens")), "gps_present": metadata.get("gps_present", False)}
    if not completeness["capture_time_recorded"] or (image and not completeness["camera_recorded"]):
        add("metadata_incomplete", completeness)
    dimensions = {"content": {"width": metadata.get("width"), "height": metadata.get("height"),
                               "format": metadata.get("format"), "integrity": "not_full_decode_verified"},
                  "metadata_completeness": completeness,
                  "time_reliability": {"judgement": "unassessed", "flags": [k for k in labels if "time" in k]},
                  "provenance": {"trust": "unassessed"}}
    return {"times": times, "labels": labels, "dimensions": dimensions}
