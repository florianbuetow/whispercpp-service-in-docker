"""Run the transcription HTTP service."""

import os
from pathlib import Path

import uvicorn

from whispercpp_transcription_api.api import create_app


def main() -> None:
    """Start one API process and one sequential worker."""
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    app = create_app(data_dir)
    uvicorn.run(app, host=os.environ.get("BIND_HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")), workers=1)


if __name__ == "__main__":
    main()
