"""Regression tests for upload path and worker lifecycle hardening."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from app import worker
from app.config import get_settings
from app.document_helpers import _stored_path
from app.errors import CorruptFile
from app.pipeline import process_document
from synth import invoices as S
from tests.test_api import upload


def test_stored_path_rejects_non_flat_and_invalid_names():
    for name in (
        "",
        ".",
        "..",
        "../outside.pdf",
        "nested/file.pdf",
        r"nested\\file.pdf",
        r"C:\\temp\\outside.pdf",
        "C:outside.pdf",
        "stream.pdf:secret",
        "\x00",
    ):
        assert _stored_path(name) is None, repr(name)


def test_stored_path_rejects_symlink_inside_upload_directory():
    uploads = get_settings().upload_dir
    uploads.mkdir(parents=True, exist_ok=True)
    target = uploads / "real-upload.pdf"
    alias = uploads / "alias-upload.pdf"
    target.write_bytes(b"synthetic")
    try:
        alias.symlink_to(target.name)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"Symlinks are unavailable in this environment: {error}")

    assert _stored_path(alias.name) is None


def test_pipeline_rejects_poisoned_stored_name(client, tmp_path, spec):
    uploaded = upload(client, S.write_pdf(spec, tmp_path / "invoice.pdf"))
    doc_id = uploaded[0]["document_id"]
    secret = tmp_path / "outside-upload.pdf"
    secret.write_bytes(b"must never be opened by the pipeline")

    import os
    from app.db import connect

    stored_name = os.path.relpath(secret, get_settings().upload_dir)
    with connect() as db:
        db.execute(
            "UPDATE documents SET stored_name=? WHERE id=?",
            (stored_name, doc_id),
        )

    with pytest.raises(CorruptFile, match="path is invalid"):
        process_document(doc_id)

    assert secret.read_bytes() == b"must never be opened by the pipeline"


def test_worker_shutdown_timeout_retains_thread_and_blocks_restart(monkeypatch):
    # Clean up any state left by another test before installing the blocker.
    worker.stop(timeout=2)
    entered = threading.Event()
    release = threading.Event()

    def blocked_run_one():
        entered.set()
        release.wait(timeout=5)
        return False

    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(workers=1),
    )
    monkeypatch.setattr(worker, "recover_stuck", lambda: 0)
    monkeypatch.setattr(worker, "run_one", blocked_run_one)

    try:
        worker.start()
        assert entered.wait(timeout=2)

        with pytest.raises(RuntimeError, match="previous threads are alive"):
            worker.start()

        with pytest.raises(TimeoutError, match="shutdown timed out"):
            worker.stop(timeout=0.01)

        assert len(worker._threads) == 1
        assert worker._threads[0].is_alive()
    finally:
        release.set()
        worker.stop(timeout=2)

    assert worker._threads == []


def test_lifespan_stops_worker_even_when_application_exits_with_error(monkeypatch):
    from app import main

    calls = []
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "bootstrap_legacy_api_key", lambda: None)
    monkeypatch.setattr(main, "bootstrap_admin", lambda: None)
    monkeypatch.setattr(main.worker, "start", lambda: calls.append("start"))
    monkeypatch.setattr(main.worker, "stop", lambda: calls.append("stop"))

    async def exercise():
        async with main.lifespan(None):
            raise RuntimeError("simulated application failure")

    with pytest.raises(RuntimeError, match="simulated application failure"):
        asyncio.run(exercise())

    assert calls == ["start", "stop"]
