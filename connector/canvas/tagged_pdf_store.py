"""Where the connector keeps the tagged PDFs Iris made (``IRIS_TAGGED_PDF_DIR``).

One folder per job, holding one file named after the Iris sessions it came
from. Files, not Redis: a tagged PDF is the size of the original document,
and Redis keeps everything in memory. The purge paths delete a job's folder
along with its Redis content (``state.forget_job_files``).
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)

_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def job_dir(job_id: str) -> Path | None:
    """The job's folder, or None when nothing is kept (no store configured,
    or an id that is not safe to use as a folder name)."""
    if not _SAFE_ID.fullmatch(job_id or ""):
        return None
    root = str(getattr(settings, "iris_tagged_pdf_dir", "") or "").strip()
    return Path(root) / job_id if root else None


def read(job_id: str, key: str) -> bytes | None:
    folder = job_dir(job_id)
    if folder is None:
        return None
    try:
        return (folder / f"{key}.pdf").read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Could not read the stored tagged PDF for %s: %s", job_id, exc)
        return None


def write(job_id: str, key: str, data: bytes) -> None:
    """Keep ``data`` as the job's tagged PDF, replacing any older one."""
    folder = job_dir(job_id)
    if folder is None:
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, folder / f"{key}.pdf")
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        for old in folder.iterdir():
            if old.name != f"{key}.pdf":
                with contextlib.suppress(OSError):
                    old.unlink()
    except OSError as exc:
        # The file is still served this time; it is made again next time.
        logger.warning("Could not store the tagged PDF for %s: %s", job_id, exc)


def forget(job_id: str) -> None:
    """Delete everything kept for the job."""
    folder = job_dir(job_id)
    if folder is not None and folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
