# Docker whisper.cpp service commands.
# Keep the default target as help. Use printf for status messages, blank lines
# around each recipe, and fail immediately when a command fails.

_default:
    @just help

# Show the available commands
help:
    @echo ""
    @printf "\033[0;34m=== docker-whispercpp-service ===\033[0m\n"
    @echo ""
    @printf "\033[0;33mSetup:\033[0m\n"
    @printf "  %-38s %s\n" "init" "Check requirements and install test tools"
    @echo ""
    @printf "\033[0;33mDocker:\033[0m\n"
    @printf "  %-38s %s\n" "build" "Force a clean image rebuild; keep host data"
    @printf "  %-38s %s\n" "docker-up" "Start the built image and wait for the API"
    @printf "  %-38s %s\n" "docker-down" "Stop containers; keep host data and models"
    @printf "  %-38s %s\n" "status" "Show container state and memory use"
    @echo ""
    @printf "\033[0;33mTranscription:\033[0m\n"
    @printf "  %-38s %s\n" "process AUDIO [LANG] [MODEL]" "Submit media and save results beside it"
    @echo ""
    @printf "\033[0;33mChecks:\033[0m\n"
    @printf "  %-38s %s\n" "test" "Run the test suite"
    @printf "  %-38s %s\n" "ci" "Run local lint, type, security, and test checks"
    @printf "\033[0;32m✓ Help displayed\033[0m\n"
    @echo ""

# Check requirements and install the project's test tools
init:
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'printf "\033[0;31m✗ Initialization failed\033[0m\n"' ERR
    echo ""
    printf "\033[0;34m=== Checking Project Requirements ===\033[0m\n"
    for tool in uv docker ffmpeg ffprobe curl jq; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            printf "\033[0;31m✗ Required command missing: %s\033[0m\n" "$tool"
            exit 1
        fi
    done
    if ! docker compose version >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
        printf "\033[0;31m✗ Docker Compose or the Docker daemon is unavailable\033[0m\n"
        exit 1
    fi
    if ! bash scripts/compose.sh config --quiet; then
        printf "\033[0;31m✗ config/service.env or compose.yaml is invalid\033[0m\n"
        exit 1
    fi
    mkdir -p data/queue data/output models
    if [ ! -w data ] || [ ! -w models ]; then
        printf "\033[0;31m✗ Host data or models folder is not writable\033[0m\n"
        exit 1
    fi
    uv sync --locked --extra dev
    printf "\033[0;32m✓ Project ready\033[0m\n"
    echo ""

# Force a clean image rebuild without touching host data
build:
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'printf "\033[0;31m✗ Docker rebuild failed\033[0m\n"' ERR
    echo ""
    printf "\033[0;34m=== Rebuilding Docker Image ===\033[0m\n"
    running="$(bash scripts/compose.sh ps --status running -q transcription)"
    if [ -n "$running" ]; then
        printf "Service is running. The rebuild will not restart its container.\n"
    else
        printf "Service is stopped.\n"
    fi
    bash scripts/compose.sh build --no-cache
    printf "\033[0;32m✓ Image rebuilt; host data preserved\033[0m\n"
    echo ""

# Start the built image, waiting until the API is healthy
docker-up:
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'printf "\033[0;31m✗ Docker service failed to start; run just build if the image is missing\033[0m\n"' ERR
    echo ""
    printf "\033[0;34m=== Starting Docker Service ===\033[0m\n"
    running="$(bash scripts/compose.sh ps --status running -q transcription)"
    if [ -n "$running" ]; then
        printf "Service is running. Checking its configuration.\n"
    else
        printf "Service is stopped. Starting it now.\n"
    fi
    bash scripts/compose.sh up --no-build --wait --wait-timeout 1800 -d
    endpoint="$(bash scripts/compose.sh port transcription 8000)"
    host="${endpoint%:*}"
    port="${endpoint##*:}"
    if [ "$host" = "0.0.0.0" ] || [ "$host" = "[::]" ]; then
        host="127.0.0.1"
    fi
    if ! curl --fail --silent --connect-timeout 1 --max-time 2 "http://$host:$port/health" | jq --exit-status '.status == "ok"' >/dev/null 2>&1; then
        if [ "$(docker context show)" = "colima" ] && [ "$host" = "127.0.0.1" ]; then
            uv run python scripts/colima_forward.py open "$port"
        fi
    fi
    reachable=false
    for attempt in {1..30}; do
        if curl --fail --silent --connect-timeout 1 --max-time 2 "http://$host:$port/health" | jq --exit-status '.status == "ok"' >/dev/null 2>&1; then
            reachable=true
            break
        fi
        sleep 1
    done
    if [ "$reachable" != true ]; then
        printf "\033[0;31m✗ API is healthy in Docker, but published port %s is unreachable from the host\033[0m\n" "$endpoint" >&2
        exit 1
    fi
    printf "\033[0;32m✓ API is healthy\033[0m\n"
    echo ""

# Stop the service without deleting the host-mounted data
docker-down:
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'printf "\033[0;31m✗ Docker service failed to stop\033[0m\n"' ERR
    echo ""
    printf "\033[0;34m=== Stopping Docker Service ===\033[0m\n"
    port="$(bash scripts/compose.sh config --format json | jq --exit-status --raw-output '.services.transcription.ports[0].published')"
    bash scripts/compose.sh down
    uv run python scripts/colima_forward.py close "$port"
    printf "\033[0;32m✓ Service stopped; host data preserved\033[0m\n"
    echo ""

# Show container state and one memory usage sample
status:
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'printf "\033[0;31m✗ Could not read Docker status\033[0m\n"' ERR
    echo ""
    printf "\033[0;34m=== Docker Status ===\033[0m\n"
    bash scripts/compose.sh ps --all
    running="$(bash scripts/compose.sh ps --status running -q transcription)"
    if [ -n "$running" ]; then
        printf "\033[0;34m=== Memory Usage ===\033[0m\n"
        bash scripts/compose.sh stats --no-stream transcription
        endpoint="$(bash scripts/compose.sh port transcription 8000)"
        host="${endpoint%:*}"
        port="${endpoint##*:}"
        if [ "$host" = "0.0.0.0" ] || [ "$host" = "[::]" ]; then
            host="127.0.0.1"
        fi
        if curl --fail --silent --connect-timeout 1 --max-time 2 "http://$host:$port/health" | jq --exit-status '.status == "ok"' >/dev/null 2>&1; then
            printf "Host API is reachable at http://%s:%s\n" "$host" "$port"
        else
            printf "\033[0;33m! Container is running, but published host port %s is unreachable\033[0m\n" "$endpoint"
        fi
    else
        printf "Service is not running; no memory usage to show.\n"
    fi
    printf "\033[0;32m✓ Status displayed\033[0m\n"
    echo ""

# Submit one audio or video file and save its results beside the source
[positional-arguments]
process file language="" model="":
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'printf "\033[0;31m✗ File processing failed\033[0m\n"' ERR
    echo ""
    printf "\033[0;34m=== Processing File ===\033[0m\n"
    bash scripts/process-file.sh "$1" "$2" "$3"
    printf "\033[0;32m✓ Transcript saved\033[0m\n"
    echo ""

# Run unit and integration tests
test:
    @echo ""
    @printf "\033[0;34m=== Running Tests ===\033[0m\n"
    @uv run --extra dev pytest -q || { printf "\033[0;31m✗ Tests failed\033[0m\n"; exit 1; }
    @printf "\033[0;32m✓ Tests passed\033[0m\n"
    @echo ""

# Check source style, types, and behavior without modifying files
ci:
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'printf "\033[0;31m✗ CI failed\033[0m\n"' ERR
    echo ""
    printf "\033[0;34m=== Running CI ===\033[0m\n"
    uv sync --locked --extra dev
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy src
    uv run bandit -ll -ii -c pyproject.toml -r src
    uv run deptry src
    uv run codespell src tests scripts *.md *.toml
    SSL_CERT_FILE="$(uv run python -c 'import certifi; print(certifi.where())')" uv run semgrep --config config/semgrep/ --error src scripts tests pyproject.toml
    uv run pytest -q
    uv run pyright --project pyrightconfig.json
    printf "\033[0;32m✓ CI passed\033[0m\n"
    echo ""
