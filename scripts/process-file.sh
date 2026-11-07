#!/usr/bin/env bash
set -euo pipefail

input="${1:?provide an audio or video file}"
language="${2:-}"
model="${3:-}"
if [ ! -f "$input" ] || [ ! -r "$input" ]; then
    printf 'Input file is missing or unreadable: %s\n' "$input" >&2
    exit 1
fi
for tool in curl jq; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'Required command missing: %s\n' "$tool" >&2
        exit 1
    fi
done

source_dir="$(cd "$(dirname "$input")" && pwd -P)"
filename="$(basename "$input")"
source="$source_dir/$filename"
stem="${filename%.*}"
if [ -z "$stem" ]; then
    stem="$filename"
fi

if ! endpoint="$(bash scripts/compose.sh port transcription 8000 2>/dev/null)" || [ -z "$endpoint" ]; then
    printf 'Service is down. Start it with: just docker-up\n' >&2
    exit 1
fi
host="${endpoint%:*}"
port="${endpoint##*:}"
if [ "$host" = "0.0.0.0" ] || [ "$host" = "[::]" ]; then
    host="127.0.0.1"
fi
base_url="http://$host:$port"
if ! curl --fail --silent --show-error --connect-timeout 3 --max-time 5 "$base_url/health" | jq --exit-status '.status == "ok"' >/dev/null; then
    printf 'Service is not healthy at %s. Check: just status\n' "$base_url" >&2
    exit 1
fi

form_args=(--form "file=@$source")
if [ -n "$language" ]; then
    form_args+=(--form-string "language=$language")
fi
if [ -n "$model" ]; then
    form_args+=(--form-string "model=$model")
fi
printf 'Uploading %s\n' "$source"
if ! response="$(curl --fail-with-body --silent --show-error "${form_args[@]}" "$base_url/jobs")"; then
    printf 'Upload rejected: %s\n' "$response" >&2
    exit 1
fi
job_id="$(printf '%s' "$response" | jq --exit-status --raw-output '.id | strings')"
if [[ ! "$job_id" =~ ^[0-9a-f-]{36}$ ]]; then
    printf 'API returned an invalid job ID\n' >&2
    exit 1
fi
printf 'Job ID: %s\n' "$job_id"

while true; do
    job="$(curl --fail --silent --show-error "$base_url/jobs/$job_id")"
    status="$(printf '%s' "$job" | jq --exit-status --raw-output '.status | strings')"
    printf 'Job %s: %s\n' "$job_id" "$status"
    case "$status" in
        todo|in_progress) sleep 3 ;;
        done)
            if [ "$(printf '%s' "$job" | jq --raw-output '.result_available')" != "true" ]; then
                printf 'Transcript for this deduplicated job was previously deleted.\n' >&2
                exit 1
            fi
            break
            ;;
        failed)
            printf 'Transcription failed: %s\n' "$(printf '%s' "$job" | jq --raw-output '.error')" >&2
            exit 1
            ;;
        *) printf 'Unexpected job status: %s\n' "$status" >&2; exit 1 ;;
    esac
done

txt="$source_dir/$stem.$job_id.txt"
srt="$source_dir/$stem.$job_id.srt"
if [ -e "$txt" ] || [ -e "$srt" ]; then
    if [ -f "$txt" ] && [ -f "$srt" ]; then
        printf 'Results already saved: %s and %s\n' "$txt" "$srt"
        exit 0
    fi
    printf 'A result path already exists; refusing to overwrite it.\n' >&2
    exit 1
fi
tmp_txt="$(mktemp "$source_dir/.$stem.$job_id.txt.XXXXXX")"
tmp_srt="$(mktemp "$source_dir/.$stem.$job_id.srt.XXXXXX")"
trap 'rm -f "$tmp_txt" "$tmp_srt"' EXIT
curl --fail --silent --show-error --output "$tmp_txt" "$base_url/jobs/$job_id/result"
curl --fail --silent --show-error --output "$tmp_srt" "$base_url/jobs/$job_id/result?format=srt"
mv "$tmp_txt" "$txt"
mv "$tmp_srt" "$srt"
printf 'Text: %s\nSRT:  %s\n' "$txt" "$srt"
