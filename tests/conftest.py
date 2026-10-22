"""Shared pytest fixtures. Populated as connector modules are ported."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_aws_metadata_probe(monkeypatch, tmp_path):  # noqa: ANN001
    """Keep tests off the network and the real disk.

    Every score payload asks whether audio can be offered, and boto3 looks
    for credentials at the EC2 metadata address when nothing else has any,
    which costs a second (or a hang) per test. Tagged PDFs Iris made go to
    a folder of this test's own.
    """
    from connector.canvas import alt_formats
    from connector.config import settings

    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setattr(alt_formats, "_audio_check", None)
    monkeypatch.setattr(settings, "iris_tagged_pdf_dir", str(tmp_path / "tagged-pdf"))
