"""Vercel entrypoint: exposes the FastAPI app to the Python serverless runtime.

The project uses a ``src/`` layout that is not pip-installed inside the
deployment sandbox, so the package path is appended before importing. Local
development keeps using ``invoice-dedupe serve``; this shim exists only for
serverless hosting (see README "Deploy to Vercel"). The deployment sets
``DEDUPE_SERVERLESS=1`` so upload handlers drain the job queue inline.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from invoice_dedupe.api import app  # noqa: E402

__all__ = ["app"]
