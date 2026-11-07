# Docker whisper.cpp service

A small HTTP service that accepts audio or video uploads, transcribes them one at a time with [whisper.cpp](https://github.com/ggml-org/whisper.cpp), and stores text and SRT results. Python handles the API and persistent queue; FFmpeg handles audio preparation. The default language is English and the default model is `medium.en` (also accepted as `en-medium`).

## Start

```sh
just init
just build
just docker-up
just status
curl http://127.0.0.1:8000/health
just docker-down
```

The first start downloads `ggml-medium.en.bin` (about 1.5 GiB) into the host models folder. The image contains a CPU build of whisper.cpp. Edit [config/service.env](config/service.env) to set the host data and models paths, published port and bind address, CPU allowance, whisper.cpp thread count, default model, and upload size limit. The project mounts that config folder read-only at `/config`. `just build` forces a clean image rebuild without deleting host files. `just docker-up` starts that image and waits for a healthy API. Configuration changes take effect after `just docker-up` recreates the container.

`just build` downloads build inputs from registries, GitHub, Debian, and PyPI, so it needs internet access. It runs the official Docker CLI in a temporary container so registry lookups use Docker's network. If an image is already built, `just docker-up` can start it without rebuilding.

The host data folder is bind-mounted at `/data` and holds `jobs.sqlite3`, pending files in `queue/`, and transcripts in `output/`. The host models folder is bind-mounted at `/models`. `just docker-down` stops the container without deleting either folder; `just docker-up` uses the same files and database. `just status` shows container state, current memory use, and whether the published host port is reachable. Allocate enough memory for the selected model and audio processing.

On Colima, `docker-up` repairs a missing localhost port forward through Colima's existing SSH control socket when the container is healthy but its published port is unreachable. `docker-down` closes only the forward created for this service.

To transcribe a local file and save text and SRT beside it, run `just process /path/to/recording.mp3`. You can add language and model: `just process /path/to/recording.mp3 nl medium`. The command requires a healthy service, prints the job ID and polling status, and writes `recording.JOB_ID.txt` and `recording.JOB_ID.srt` in the source folder. It preserves existing result files.

```sh
curl -F 'file=@recording.mp3' http://127.0.0.1:8000/jobs
curl -F 'file=@recording.mp3' -F 'language=nl' -F 'model=medium' http://127.0.0.1:8000/jobs
curl http://127.0.0.1:8000/jobs
curl http://127.0.0.1:8000/jobs/JOB_ID
curl http://127.0.0.1:8000/jobs/JOB_ID/result
curl 'http://127.0.0.1:8000/jobs/JOB_ID/result?format=srt'
curl -X DELETE http://127.0.0.1:8000/jobs/JOB_ID/result
```

`POST /jobs` returns a job ID and `deduplicated` flag. A repeat upload with identical file bytes, language, and model returns the original ID. The list and individual status include submission/start/finish time, input bytes, audio duration, output bytes, filename, settings, hash, status (`todo`, `in_progress`, `done`, or `failed`), and result availability. The worker retries jobs interrupted by a container restart. A failed job is requeued when the same file is submitted again. Results can be removed, but the hash and job metadata stay in the index, so that upload remains deduplicated and returns a `410` result response.

The service converts uploads to 16 kHz mono PCM WAV, detects silence below −40 dB lasting at least 1 second, concatenates speech segments, and restores SRT timestamps to the original timeline. An entirely silent recording produces empty text and SRT files. Input files are deleted after results are complete; failed inputs stay for retry. Output files are `data/output/JOB_ID.txt` and `data/output/JOB_ID.srt`.

When a job requests another model, the worker downloads its `ggml-MODEL.bin` file into the host models folder before transcription. You can preinstall one with `docker compose --env-file config/service.env exec transcription /app/scripts/download-model.sh small.en`. English-only `.en` models require `language=en`; other languages need a multilingual model. The model field uses whisper.cpp model names such as `medium`, `small.en`, or `large-v3`. The service does not translate to English; `language` specifies the spoken language.

The API binds to localhost by default and has no authentication. Put it behind an authenticated reverse proxy before exposing it to a network. `MAX_UPLOAD_BYTES` defaults to 1 GiB. Run exactly one API process or container against a data directory to preserve sequential processing.

## Develop

```sh
just test
just ci
```

The Justfile uses Docker Compose for the service. For direct local development, run `DATA_DIR=./data MODEL_DIR=./models WHISPER_BIN=/path/to/whisper-cli uv run src/main.py`.

For a speech sample on macOS, `say -v Daniel -o /tmp/transcription-test.aiff 'The quick brown fox jumps over the lazy dog.'` followed by `ffmpeg -i /tmp/transcription-test.aiff -ar 16000 -ac 1 -c:a pcm_s16le /tmp/transcription-test.wav` creates a WAV to upload. The installed Daniel voice on the test host rejected direct WAVE output, so the intermediate AIFF is needed there.
