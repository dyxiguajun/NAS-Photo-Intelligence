import json
import os
from pathlib import Path, PurePosixPath


def relative(value, allow_empty=False):
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise ValueError("Invalid relative path")
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or (str(p) == "." and not allow_empty):
        raise ValueError("Path must remain inside its configured source")
    return "" if str(p) == "." else str(p)


class Config:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.raw = json.loads(self.path.read_text())
        self.project = self.path.parent.parent.resolve()
        import subprocess
        try:
            result = subprocess.run(["mount"], capture_output=True, text=True, timeout=5)
        except subprocess.TimeoutExpired:
            try:
                result = subprocess.run(["mount"], capture_output=True, text=True, timeout=10)
            except subprocess.TimeoutExpired as error:
                raise ValueError("Unable to verify local workspace mounts") from error
        if result.returncode:
            raise ValueError("Unable to verify local workspace mounts")
        network_mounts = []
        for line in result.stdout.splitlines():
            if " on " in line and any(t in line.lower() for t in ["smbfs", "cifs", " nfs", "(nfs"]):
                network_mounts.append(Path(line.split(" on ", 1)[1].split(" (", 1)[0]))
        if any(self.project.is_relative_to(m) for m in network_mounts):
            raise ValueError("SQLite workspace cannot reside on a network mount")
        self.sources = {}
        seen = set()
        for s in self.raw["sources"]:
            s = dict(s)
            s["logical_root"] = relative(s["logical_root"], True)
            identity = (s["device_id"], s["logical_root"])
            if s["source_id"] in self.sources or identity in seen:
                raise ValueError("Duplicate source identity")
            if not s["source_id"] or not s["device_id"]:
                raise ValueError("Empty source or device identity")
            seen.add(identity)
            device = self.raw["devices"][s["device_id"]]
            mount = Path(device["mount_root"])
            if not mount.is_absolute():
                mount = self.project / mount
            s["root"] = os.path.abspath(mount / s["logical_root"])
            # Resolve aliases on local sources (e.g. macOS /var -> /private/var).
            # Do not perform potentially blocking network path resolution here.
            if not any(Path(s["root"]).is_relative_to(m) for m in network_mounts):
                s["root"] = str(Path(s["root"]).resolve(strict=False))
            self.sources[s["source_id"]] = s
        self.rules = {"early_filesystem_before": "2000-01-01", "future_tolerance_days": 30,
                      "conflict_tolerance_hours": 24, **self.raw.get("rules", {})}
        # Validate even if no file happens to exercise a rule.
        from datetime import date
        date.fromisoformat(self.rules["early_filesystem_before"])
        if not 0 <= self.rules["future_tolerance_days"] <= 3650:
            raise ValueError("Invalid future tolerance")
        if not 0 <= self.rules["conflict_tolerance_hours"] <= 87600:
            raise ValueError("Invalid conflict tolerance")
        self.limits = {"max_files": 200, "file_timeout_seconds": 45,
                       "max_attempts": 2, "run_budget_seconds": 900,
                       "max_file_bytes": 536870912, **self.raw.get("limits", {})}
        if not 1 <= self.limits["max_files"] <= 200:
            raise ValueError("Phase 1 limit is 1..200 files per manifest")
        if not 0.1 <= self.limits["file_timeout_seconds"] <= 300:
            raise ValueError("Invalid file timeout")
        if not 1 <= self.limits["max_attempts"] <= 3:
            raise ValueError("Retries must be bounded (1..3 attempts)")
        if not 0.1 <= self.limits["run_budget_seconds"] <= 3600:
            raise ValueError("Run budget must be bounded")
        self.outputs = {}
        for name, default in {"database": "data/index.sqlite3", "logs": "logs",
                              "cache": "cache", "reports": "reports"}.items():
            candidate = self.project / self.raw.get("paths", {}).get(name, default)
            if any(Path(os.path.abspath(candidate)).is_relative_to(m) for m in network_mounts):
                raise ValueError("Output path cannot reside on a network mount")
            target = candidate.resolve()
            if any(target.is_relative_to(m) for m in network_mounts):
                raise ValueError("Output path cannot reside on a network mount")
            if not target.is_relative_to(self.project) or str(target).startswith("/Volumes/"):
                raise ValueError("Outputs must be on the local project filesystem")
            for s in self.sources.values():
                if target.is_relative_to(Path(s["root"])):
                    raise ValueError("Output overlaps an input source")
            self.outputs[name] = target
        self.exiftool = self._tool(self.raw.get("tools", {}).get("exiftool", "tools/exiftool/exiftool"))
        self.ffprobe = self.raw.get("tools", {}).get("ffprobe", "ffprobe")

    def _tool(self, value):
        return str(self.project / value) if "/" in value and not value.startswith("/") else value

    def manifest(self, path):
        if Path(path).stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Manifest too large")
        items = json.loads(Path(path).read_text())
        if not isinstance(items, list) or not 1 <= len(items) <= self.limits["max_files"]:
            raise ValueError("Manifest must contain 1..max_files explicit entries")
        result, seen = [], set()
        for item in items:
            sid = item["source_id"]
            if sid not in self.sources:
                raise ValueError("Unknown source_id")
            rel = relative(item["relative_path"])
            if (sid, rel) in seen:
                continue
            seen.add((sid, rel))
            prior = item.get("prior_evidence", {})
            if len(json.dumps(prior)) > 65536:
                raise ValueError("Prior evidence too large")
            result.append({"source_id": sid, "relative_path": rel, "prior_evidence": prior})
        return result
