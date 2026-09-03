from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import requests
from pydantic import ValidationError

from .models import PublicPaperReport


DEFAULT_REPORT_PATH = Path(__file__).resolve().parents[2] / "data" / "public_report.example.json"
MAX_REPORT_BYTES = 2_000_000
PUBLIC_ERROR_MESSAGE = "Data temporarily unavailable"


class PublicReportUnavailable(RuntimeError):
    """Raised without internal details when sanitized reporting is unavailable."""


def parse_public_report(payload: bytes) -> PublicPaperReport:
    """Validate bytes against the strict, public-safe reporting contract."""
    if not payload or len(payload) > MAX_REPORT_BYTES:
        raise PublicReportUnavailable(PUBLIC_ERROR_MESSAGE)
    try:
        raw = json.loads(payload.decode("utf-8"))
        return PublicPaperReport.model_validate(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        raise PublicReportUnavailable(PUBLIC_ERROR_MESSAGE) from exc


def configured_source() -> tuple[str, str]:
    """Return operator-controlled source configuration; visitors cannot set it."""
    return (
        os.environ.get("PUBLIC_REPORT_URL", "").strip(),
        os.environ.get("PUBLIC_REPORT_ALLOWED_HOST", "").strip().lower(),
    )


def _validate_source_url(source_url: str, allowed_host: str) -> None:
    try:
        parsed = urlsplit(source_url)
        port = parsed.port
    except ValueError as exc:
        raise PublicReportUnavailable(PUBLIC_ERROR_MESSAGE) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.lower() != allowed_host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise PublicReportUnavailable(PUBLIC_ERROR_MESSAGE)


def load_public_report(
    source_url: str = "",
    allowed_host: str = "",
    *,
    local_path: Path = DEFAULT_REPORT_PATH,
) -> PublicPaperReport:
    """Load a sanitized report from one fixed HTTPS host or the local sample."""
    if not source_url and not allowed_host:
        try:
            return parse_public_report(local_path.read_bytes())
        except (OSError, PublicReportUnavailable) as exc:
            raise PublicReportUnavailable(PUBLIC_ERROR_MESSAGE) from exc
    if not source_url or not allowed_host:
        raise PublicReportUnavailable(PUBLIC_ERROR_MESSAGE)

    _validate_source_url(source_url, allowed_host)
    response = None
    try:
        response = requests.get(
            source_url,
            headers={"Accept": "application/json", "User-Agent": "paper-observability-dashboard/1"},
            timeout=(3.05, 8),
            allow_redirects=False,
            stream=True,
        )
        if response.status_code != 200:
            raise PublicReportUnavailable(PUBLIC_ERROR_MESSAGE)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"application/json", "text/json"}:
            raise PublicReportUnavailable(PUBLIC_ERROR_MESSAGE)
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_REPORT_BYTES:
                    raise PublicReportUnavailable(PUBLIC_ERROR_MESSAGE)
            except ValueError as exc:
                raise PublicReportUnavailable(PUBLIC_ERROR_MESSAGE) from exc
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_REPORT_BYTES:
                raise PublicReportUnavailable(PUBLIC_ERROR_MESSAGE)
            chunks.append(chunk)
        return parse_public_report(b"".join(chunks))
    except (requests.RequestException, PublicReportUnavailable) as exc:
        raise PublicReportUnavailable(PUBLIC_ERROR_MESSAGE) from exc
    finally:
        if response is not None:
            response.close()
