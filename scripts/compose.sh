#!/usr/bin/env bash
# Use the current Docker context even if its configured credential helper is absent.
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
docker_config_dir="${DOCKER_CONFIG:-$HOME/.docker}"
config_file="$docker_config_dir/config.json"

if [ -f "$config_file" ]; then
    helper="$(sed -n 's/.*"credsStore"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$config_file")"
    if [ -n "$helper" ] && ! command -v "docker-credential-$helper" >/dev/null 2>&1; then
        if [ -z "${DOCKER_HOST:-}" ]; then
            docker_host="$(docker context inspect --format '{{.Endpoints.docker.Host}}')"
            if [ -z "$docker_host" ]; then
                printf 'Cannot resolve the current Docker context\n' >&2
                exit 1
            fi
            export DOCKER_HOST="$docker_host"
        fi
        temporary_docker_config="$(mktemp -d)"
        trap 'rm -rf "$temporary_docker_config"' EXIT
        printf '{"auths":{}}\n' > "$temporary_docker_config/config.json"
        if [ -d "$docker_config_dir/cli-plugins" ]; then
            mkdir -p "$temporary_docker_config/cli-plugins"
            for plugin in "$docker_config_dir"/cli-plugins/*; do
                if [ -e "$plugin" ]; then
                    ln -s "$plugin" "$temporary_docker_config/cli-plugins/$(basename "$plugin")"
                fi
            done
        fi
        export DOCKER_CONFIG="$temporary_docker_config"
    fi
fi

if [ "${1:-}" = "build" ]; then
    if ! docker image inspect docker:cli >/dev/null 2>&1; then
        docker pull docker:cli >/dev/null
    fi
    project_name="${COMPOSE_PROJECT_NAME:-$(basename "$project_dir")}"
    docker_args=(
        --rm --pull=never
        -e "COMPOSE_PROJECT_NAME=$project_name"
        -v /var/run/docker.sock:/var/run/docker.sock
        -v "$project_dir:/workspace"
        -w /workspace
    )
    for variable in HOST_DATA_DIR HOST_MODELS_DIR HOST_PORT HOST_BIND_ADDRESS CPU_CORES DEFAULT_MODEL WHISPER_THREADS; do
        if [ -n "${!variable:-}" ]; then
            docker_args+=(-e "$variable=${!variable}")
        fi
    done
    docker run "${docker_args[@]}" docker:cli docker compose --env-file config/service.env "$@"
else
    docker compose --env-file config/service.env "$@"
fi
