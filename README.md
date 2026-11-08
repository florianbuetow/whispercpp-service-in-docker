# Whisper.cpp Transcription Service in Docker

A local HTTP service for transcribing audio and video with
[whisper.cpp](https://github.com/ggml-org/whisper.cpp). Upload a file, receive a job ID, and
fetch a text transcript or SRT subtitles when it finishes. One worker processes jobs in
submission order. FFmpeg converts media to 16 kHz mono audio, removes long silence, and restores
SRT timestamps to the original recording.

The default is **English** with **`medium.en`**. The Docker image runs on a slim Debian base
with CPU inference. Models, queued uploads, transcripts, and the SQLite job index live in
host-mounted folders.

## Requirements

- Docker with the Compose plugin and a running Docker daemon
- `just`, `uv`, `ffmpeg`, `ffprobe`, `curl`, and `jq` on the host
- Enough free disk space for the image, uploaded media, and model files. The default `medium.en`
  model is roughly 1.5 GiB and needs several GiB of available memory while running.
- Internet access for the first image build and for downloading a model that is not already in
  `models/`

`just init` checks the commands and Compose configuration, prepares the host folders, and
installs the Python development tools from `uv.lock`.

## Quick start

Run these commands from the project root:

```sh
just init
just build
just docker-up
just status
just process /path/to/recording.mp3
```

`just docker-up` waits until the API is reachable on the published host port. With the default
configuration, the API is at `http://127.0.0.1:8000`; interactive API documentation is available
at `/docs`.

To stop the service:

```sh
just docker-down
```

Stopping or rebuilding the container does **not** delete the host-mounted database, queued
inputs, transcripts, or models. `just build` forces a no-cache image rebuild but does not
restart an already running container. Run `just docker-up` afterward to use the rebuilt image.

### Transcribe a local file

```sh
just process /path/to/interview.m4a
just process /path/to/interview.m4a nl medium
```

The optional arguments are spoken language and model. The second example uses the multilingual
`medium` model for Dutch. `process` checks that the service is healthy, uploads the file, prints
the job ID and status while polling, and saves both results beside the source:

```text
/path/to/interview.m4a
/path/to/interview.JOB_ID.txt
/path/to/interview.JOB_ID.srt
```

It accepts media with a finite duration that FFmpeg can decode. Existing result files are not
overwritten. If both files already exist for a deduplicated job, it reports their paths.

## Commands

| Command | What it does |
| --- | --- |
| `just` or `just help` | Show the available commands |
| `just init` | Check requirements and install development dependencies |
| `just build` | Force a clean image rebuild; leave host data intact |
| `just docker-up` | Start the built image and wait for a reachable API |
| `just docker-down` | Stop the service and keep all host-mounted files |
| `just status` | Show container state, memory use, and host API reachability |
| `just process FILE [LANG] [MODEL]` | Submit media, poll the job, and save text and SRT beside the source |
| `just test` | Run the test suite |
| `just ci` | Run local lint, type, security, and test checks |

## Configuration

Edit [`config/service.env`](config/service.env), then run `just docker-up` to apply container
configuration changes. The `HOST_*` paths and port are resolved by Docker Compose; the remaining
settings are passed to the service.

| Setting | Default | Purpose |
| --- | --- | --- |
| `HOST_DATA_DIR` | `./data` | Host folder for SQLite, pending uploads, and results |
| `HOST_MODELS_DIR` | `./models` | Host folder for downloaded whisper.cpp models |
| `HOST_PORT` | `8000` | Published HTTP port |
| `HOST_BIND_ADDRESS` | `127.0.0.1` | Host interface that accepts HTTP connections |
| `CPU_CORES` | `4` | Docker CPU allowance |
| `WHISPER_THREADS` | `4` | Threads passed to whisper.cpp for each transcription |
| `DEFAULT_MODEL` | `medium.en` | Model used when a request does not specify one; downloaded at startup |
| `MAX_UPLOAD_BYTES` | `1073741824` | Maximum upload size in bytes (1 GiB) |

`DATA_DIR=/data`, `MODEL_DIR=/models`, and `BIND_HOST=0.0.0.0` are paths and bind settings
**inside** the container. Compose mounts the configured host folders at `/data` and `/models`;
leave those internal values as supplied. Model files follow whisper.cpp's `ggml-MODEL.bin`
naming scheme. For example, `model=small.en` uses `models/ggml-small.en.bin`. A requested model
is downloaded into the host models folder if missing. English-only `.en` models require
`language=en`; other languages need a multilingual model such as `medium`.

The service **transcribes in the selected spoken language**. It does not translate non-English
speech into English. `en-medium` is accepted as an alias for `medium.en`.

## HTTP API

Use `http://127.0.0.1:8000` below, or replace the port with your `HOST_PORT` value. Uploads use
multipart form data with a required `file` and optional `language` and `model` fields.

```sh
curl -fsS -F 'file=@recording.mp3' http://127.0.0.1:8000/jobs
curl -fsS -F 'file=@recording.mp3' -F 'language=nl' -F 'model=medium' http://127.0.0.1:8000/jobs
```

The response contains `id`, `status`, and `deduplicated` alongside the job metadata. A repeated
upload with the same file bytes, language, and model returns the existing ID with
`deduplicated: true`.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Health check |
| `POST` | `/jobs` | Submit a file; returns HTTP `201`, including for a duplicate |
| `GET` | `/jobs` | List all jobs in submission order (`count` and `jobs`) |
| `GET` | `/jobs/{id}` | Query one job's status and metadata |
| `GET` | `/jobs/{id}/result` | Download the UTF-8 text transcript |
| `GET` | `/jobs/{id}/result?format=srt` | Download SRT subtitles |
| `DELETE` | `/jobs/{id}/result` | Delete both result files while retaining the job and hash |

For example, after obtaining an ID:

```sh
curl -fsS http://127.0.0.1:8000/jobs/JOB_ID
curl -fsS http://127.0.0.1:8000/jobs/JOB_ID/result -o transcript.txt
curl -fsS 'http://127.0.0.1:8000/jobs/JOB_ID/result?format=srt' -o transcript.srt
curl -X DELETE http://127.0.0.1:8000/jobs/JOB_ID/result
```

A status response includes `status` (`todo`, `in_progress`, `done`, or `failed`),
`submitted_at`, `started_at`, `finished_at`, `filename`, `language`, `model`, `content_hash`,
`input_bytes`, `duration_seconds`, `output_bytes`, `result_available`, and `error`. Duration is
available as soon as an upload is accepted. Output size is available after completion.

Unknown IDs return `404`. Results requested before completion return `409`. A deleted result
returns `410`. Invalid media or settings return `400`; an upload beyond `MAX_UPLOAD_BYTES`
returns `413`.

### Deduplication and deletion

The deduplication key includes the uploaded file's SHA-256 hash, language, and model. Changing
the language or model creates a separate job. Successful input files are removed from the queue,
but the hash and job metadata remain in SQLite. Deleting a result removes its `.txt` and `.srt`
files **without removing the deduplication record**. Reuploading the same bytes with the same
settings then returns the original completed job ID, but its result remains unavailable (`410`).

## Storage and processing

```text
config/service.env          Host-side configuration (mounted read-only at /config)
data/jobs.sqlite3           Persistent job index and deduplication records
data/queue/JOB_ID.upload    Pending or failed source uploads
data/output/JOB_ID.txt      Text transcript
data/output/JOB_ID.srt      SRT subtitles with original timestamps
models/ggml-MODEL.bin      Downloaded model files
```

The worker first converts media to 16 kHz mono PCM WAV. FFmpeg detects silence below **-40 dB**
lasting at least **1 second**; speech segments are concatenated before whisper.cpp runs. SRT
times are mapped back to the source recording. Entirely silent recordings produce empty text and
SRT files.

There is one transcription worker and one API process per data directory. A container restart
requeues any job that was `in_progress`. A failed job keeps its uploaded file and an `error` in
its status; submitting the same file and settings again requeues it. Do not run multiple service
replicas against one data directory.

## Troubleshooting

- **`docker-up` cannot find an image:** run `just build` first. The build needs registry,
  GitHub, Debian, and PyPI access. It runs an official Docker CLI container so registry lookups
  use Docker's network.
- **Startup waits on the model:** the first start downloads `medium.en` to `models/`. Check
  `just status` and available disk space. Later starts reuse the file.
- **Job status is `failed`:** inspect `GET /jobs/{id}` for `error`. Check that FFmpeg can decode
  the source and that the model download succeeded. Reuploading the same file and settings
  retries a failed job while its queued input exists.
- **Container is healthy but the host port is unreachable:** check `HOST_PORT` and
  `HOST_BIND_ADDRESS` in `config/service.env`, then run `just docker-up`. On Colima, this
  command can repair a missing localhost forward through Colima's existing SSH control socket.
  `docker-down` closes only the forward it created.
- **A duplicate returns `410` for its result:** its transcript was deleted, but its
  deduplication record was retained by design.

The API has no authentication and binds to localhost by default. Put an authenticated reverse
proxy in front of it before changing `HOST_BIND_ADDRESS` to a network-facing interface.

## Development and smoke test

```sh
just test
just ci
```

`ci` runs Ruff, Mypy, Bandit, Deptry, Codespell, local Semgrep rules, Pytest, and Pyright. For
direct local development, set the data, model, and whisper.cpp paths:

```sh
DATA_DIR=./data MODEL_DIR=./models WHISPER_BIN=/path/to/whisper-cli uv run src/main.py
```

On macOS, a spoken test recording can be made with `say`. Some installed voices only write AIFF,
so convert that output to WAV with FFmpeg:

```sh
say -v Daniel -o /tmp/whisper-test.aiff 'The quick brown fox jumps over the lazy dog.'
ffmpeg -hide_banner -loglevel error -y -i /tmp/whisper-test.aiff -ar 16000 -ac 1 -c:a pcm_s16le /tmp/whisper-test.wav
just process /tmp/whisper-test.wav
```
