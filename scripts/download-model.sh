#!/bin/sh
set -eu
model="${1:-medium.en}"
case "$model" in
  *[!a-zA-Z0-9._-]*|.*|"") printf 'Invalid model name\n' >&2; exit 1 ;;
esac
model_dir="${MODEL_DIR:-/models}"
mkdir -p "$model_dir"
target="$model_dir/ggml-$model.bin"
if [ -s "$target" ]; then
  exit 0
fi
temporary="$target.download"
trap 'rm -f "$temporary"' EXIT HUP INT TERM
curl --fail --location --retry 3 --output "$temporary" \
  "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-$model.bin"
test -s "$temporary"
mv "$temporary" "$target"
trap - EXIT HUP INT TERM
