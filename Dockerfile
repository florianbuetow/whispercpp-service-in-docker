FROM debian:bookworm-slim AS whisper-build
ARG WHISPER_VERSION=v1.9.5
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates cmake g++ git make \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${WHISPER_VERSION}" https://github.com/ggml-org/whisper.cpp.git /src/whisper \
    && cmake -S /src/whisper -B /src/whisper/build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DWHISPER_BUILD_TESTS=OFF -DGGML_NATIVE=OFF \
    && cmake --build /src/whisper/build --config Release --target whisper-cli -j 2

FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /usr/local/bin/
COPY --from=whisper-build /src/whisper/build/bin/whisper-cli /usr/local/bin/whisper-cli
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
COPY scripts/download-model.sh scripts/entrypoint.sh ./scripts/
RUN uv sync --frozen --no-dev --no-install-project
ENV DATA_DIR=/data MODEL_DIR=/models WHISPER_BIN=/usr/local/bin/whisper-cli PATH="/app/.venv/bin:${PATH}"
RUN mkdir -p /data /models && chmod +x /app/scripts/*.sh
EXPOSE 8000
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
