#!/bin/sh
# Execute on the NAS as a Docker-authorized operator.
set -eu
workspace=${NAS_EVIDENCE_WORKSPACE:?Set the independent workspace}
state_volume=${NAS_EVIDENCE_STATE_VOLUME:?Set the workspace volume mount point}
account=${NAS_EVIDENCE_USER:?Set an unprivileged numeric uid:gid}
job=${1:?Manifest SHA-256 required}
image=${2:?Pinned local image ID required}
mode=${3:?Specify fixture or media}
case "$job" in
    ''|*[!0-9a-f]*) exit 2 ;;
esac
[ "${#job}" -eq 64 ]
case "$image" in
    sha256:*) ;;
    *) exit 2 ;;
esac
[ "${#image}" -eq 71 ]
case "$account" in
    ''|0:*|*[!0-9:]*|*:*:*) exit 2 ;;
    *:*) ;;
    *) exit 2 ;;
esac
[ "${account%%:*}" -gt 0 ]
[ "${account#*:}" -gt 0 ]
mountpoint -q "$state_volume"
[ "$(realpath "$workspace")" = "$workspace" ]
case "$workspace" in "$state_volume"/*) ;; *) exit 2 ;; esac
stage="$workspace/$job"
logical=$(python3 -B -c '
import hashlib, json, sys
from pathlib import Path, PurePosixPath
stage = Path(sys.argv[1])
raw = (stage / "manifest.json").read_bytes()
if stage.resolve() != stage or hashlib.sha256(raw).hexdigest() != stage.name:
    raise ValueError("Manifest does not match the job directory")
path = json.loads(raw)["logical_root"]
parts = PurePosixPath(path)
if parts.is_absolute() or not parts.parts or ".." in parts.parts or str(parts) != path or "," in path:
    raise ValueError("Unsafe media root")
print(path)
' "$stage")
case "$mode" in
    fixture) source_root="$workspace/fixtures" ;;
    media)
        mountpoint -q "${NAS_EVIDENCE_MEDIA_VOLUME:?Set the media volume mount point}"
        [ "$state_volume" != "$NAS_EVIDENCE_MEDIA_VOLUME" ]
        source_root=${NAS_EVIDENCE_MEDIA_ROOT:?Set the media root}
        case "$source_root" in "$NAS_EVIDENCE_MEDIA_VOLUME"/*) ;; *) exit 2 ;; esac
        ;;
    *) exit 2 ;;
esac
[ "$(realpath "$source_root/$logical")" = "$source_root/$logical" ]
[ -d "$source_root/$logical" ]
[ -f "$workspace/.worker.lock" ]
docker run --detach --init --pull never --name "nas-photo-evidence-$(printf '%.16s' "$job")" \
    --label nas-photo.project=evidence --restart unless-stopped \
    --network none --read-only --user "$account" --cap-drop ALL \
    --security-opt no-new-privileges:true --pids-limit 32 --cpus 0.65 --memory 512m --memory-swap 512m \
    --log-opt max-size=1m --log-opt max-file=2 \
    --env PYTHONDONTWRITEBYTECODE=1 --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m \
    --mount "type=bind,src=$source_root/$logical,dst=/media/$logical,readonly,bind-propagation=rprivate,bind-recursive=readonly" \
    --mount "type=bind,src=$workspace/supervise.py,dst=/supervise.py,readonly" \
    --mount "type=bind,src=$workspace/.worker.lock,dst=/work/.worker.lock" \
    --mount "type=bind,src=$stage,dst=/work/$job" \
    "$image" nice -n 12 python3 -B /supervise.py "/work/$job/manifest.json"
