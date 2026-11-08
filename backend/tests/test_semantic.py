"""Semantic search plumbing, tested with a deterministic fake embedder (no model download needed).
The real GloVe backend is exercised only when its prepared files exist."""
import hashlib
import random

import numpy as np
import pytest

from app import semantic, worker
from synth import invoices as S
from tests.test_api import upload

SYNONYMS = {"toner": "ink", "cartridge": "ink", "gloves": "hand", "protective": "hand", "rack": "cabinet", "cabinet": "cabinet"}


class FakeEmbedder:
    """Bag of 'concepts' hashed into 64 dims; a tiny synonym table lets non-identical words match."""
    dim, name = 64, "fake-64"

    def embed(self, texts):
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for r, t in enumerate(texts):
            for w in semantic.TOKEN_RE.findall(t.lower()):
                w = SYNONYMS.get(w, w)
                out[r, int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1
            n = np.linalg.norm(out[r])
            if n:
                out[r] /= n
        return out


@pytest.fixture
def sem(monkeypatch):
    monkeypatch.setenv("DOCINTEL_SEMANTIC", "glove")          # "enabled"; the embedder itself is replaced below
    monkeypatch.setenv("DOCINTEL_CHUNK_CHARS", "120")         # small chunks so a hit localises to the matching lines
    semantic.reset_embedder()
    monkeypatch.setattr(semantic, "_embedder", FakeEmbedder())
    yield
    semantic.reset_embedder()


def spec_with(items, n, supplier="Acme Supplies Ltd"):
    s = S.random_spec(random.Random(n), n, 1)
    s.items = [(d, 1, S.Decimal("10.00")) for d in items]; s.supplier = supplier
    return s


def test_chunking_groups_lines_and_respects_size():
    text = "\n".join(f"line number {i} with some words" for i in range(40))
    chunks = semantic.chunk_text(text, 200)
    assert len(chunks) > 3 and all(len(c) <= 260 for c in chunks)
    assert " ".join(chunks).count("line number") == 40
    assert semantic.chunk_text("   \n\n", 200) == []
    assert semantic.chunk_text("x" * 500, 200) == ["x" * 500]


def test_disabled_by_default_returns_503_and_keyword_search_still_works(client, tmp_path, spec):
    upload(client, S.write_pdf(spec, tmp_path / "a.pdf")); worker.run_all()
    assert client.get("/api/search/semantic", params={"q": "anything"}).status_code == 503
    assert client.post("/api/semantic/reindex").status_code == 503
    assert client.get("/api/semantic/status").json()["enabled"] is False
    assert len(client.get("/api/search", params={"q": spec.supplier.split()[0]}).json()["results"]) == 1
    from app.db import connect
    with connect() as db:
        assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0


def test_documents_are_embedded_and_found_by_meaning(client, tmp_path, sem):
    upload(client, S.write_pdf(spec_with(["Printer toner", "Support plan"], 1), tmp_path / "print.pdf"),
           S.write_pdf(spec_with(["Safety gloves", "Support plan"], 2), tmp_path / "safety.pdf"),
           S.write_pdf(spec_with(["Server rack", "Support plan"], 3), tmp_path / "rack.pdf"))
    worker.run_all()
    ev = [h["event"] for h in client.get("/api/documents/1").json()["history"]]
    assert "embedded" in ev
    r = client.get("/api/search/semantic", params={"q": "ink cartridge"}).json()
    assert r["model"] == "fake-64" and r["results"][0]["filename"] == "print.pdf"
    assert client.get("/api/search/semantic", params={"q": "protective hand wear"}).json()["results"][0]["filename"] == "safety.pdf"
    assert client.get("/api/search/semantic", params={"q": "cabinet"}).json()["results"][0]["filename"] == "rack.pdf"
    top = client.get("/api/search/semantic", params={"q": "ink cartridge"}).json()["results"][0]
    assert "toner" in top["snippets"][0]["snippet"].lower() and top["invoice_number"] and top["score"] > 0


def test_filters_apply_to_semantic_results(client, tmp_path, sem):
    upload(client, S.write_pdf(spec_with(["Printer toner"], 1, "Zephyr Aerospace"), tmp_path / "a.pdf"),
           S.write_pdf(spec_with(["Printer toner"], 2, "Borealis Foods"), tmp_path / "b.pdf"))
    worker.run_all()
    res = client.get("/api/search/semantic", params={"q": "ink", "supplier": "borealis"}).json()["results"]
    assert [r["filename"] for r in res] == ["b.pdf"]
    assert client.get("/api/search/semantic", params={"q": "ink", "currency": "XXX"}).json()["results"] == []



def test_reprocessing_preserves_archived_chunks_and_refreshes_index(
    client, tmp_path, sem
):
    upload(
        client,
        S.write_pdf(
            spec_with(["Printer toner"], 1),
            tmp_path / "a.pdf",
        ),
    )
    worker.run_all()

    from app.db import connect

    with connect() as db:
        before = db.execute(
            """
            SELECT
                COUNT(*),
                MIN(id),
                MIN(generation_id),
                MAX(generation_id)
            FROM chunks
            """
        ).fetchone()

    assert before[0] > 0

    response = client.post("/api/documents/1/reprocess")
    assert response.status_code == 202
    worker.run_all()

    with connect() as db:
        after = db.execute(
            """
            SELECT
                COUNT(*),
                MIN(id),
                MIN(generation_id),
                MAX(generation_id)
            FROM chunks
            """
        ).fetchone()

        active = db.execute(
            """
            SELECT active_generation
            FROM documents
            WHERE id=1
            """
        ).fetchone()["active_generation"]

        active_chunks = db.execute(
            """
            SELECT COUNT(*)
            FROM chunks
            WHERE document_id=1 AND generation_id=?
            """,
            (active,),
        ).fetchone()[0]

    # Old chunks remain archived; new chunks belong to a new generation.
    assert after[0] > before[0]
    assert after[1] == before[1]
    assert after[3] > before[3]

    # The active generation contains the newly generated chunks.
    assert active == after[3]
    assert active_chunks == before[0]

    # Search must return the document using its active generation.
    results = client.get(
        "/api/search/semantic",
        params={"q": "ink"},
    ).json()["results"]

    assert results
    assert results[0]["filename"] == "a.pdf"
    assert len([r for r in results if r["filename"] == "a.pdf"]) == 1


def test_reindex_backfills_documents_processed_while_disabled(client, tmp_path, monkeypatch):
    upload(client, S.write_pdf(spec_with(["Printer toner"], 1), tmp_path / "old.pdf")); worker.run_all()   # semantic off
    monkeypatch.setenv("DOCINTEL_SEMANTIC", "glove"); semantic.reset_embedder(); monkeypatch.setattr(semantic, "_embedder", FakeEmbedder())
    assert client.get("/api/semantic/status").json()["documents_embedded"] == 0
    assert client.post("/api/semantic/reindex").status_code == 202
    import time
    for _ in range(100):
        st = client.get("/api/semantic/status").json()
        if not st["reindex"]["running"]:
            break
        time.sleep(0.05)
    assert st["reindex"]["error"] is None and st["documents_embedded"] == 1 and st["chunks"] > 0
    assert client.get("/api/search/semantic", params={"q": "ink"}).json()["results"][0]["filename"] == "old.pdf"
    semantic.reset_embedder()


def test_embedding_failure_never_fails_the_document(client, tmp_path, spec, monkeypatch):
    class Broken(FakeEmbedder):
        def embed(self, texts): raise RuntimeError("model exploded")
    monkeypatch.setenv("DOCINTEL_SEMANTIC", "glove"); semantic.reset_embedder(); monkeypatch.setattr(semantic, "_embedder", Broken())
    upload(client, S.write_pdf(spec, tmp_path / "a.pdf")); worker.run_all()
    d = client.get("/api/documents/1").json()
    assert d["status"] == "completed" and d["invoices"][0]["invoice_number"] == spec.number
    assert any(h["event"] == "embedding_failed" and "exploded" in h["detail"] for h in d["history"])
    semantic.reset_embedder()


def test_chunks_from_another_model_are_ignored(client, tmp_path, monkeypatch, sem):
    upload(client, S.write_pdf(spec_with(["Printer toner"], 1), tmp_path / "a.pdf")); worker.run_all()
    class Other(FakeEmbedder):
        name = "other-model"
    monkeypatch.setattr(semantic, "_embedder", Other())
    assert client.get("/api/search/semantic", params={"q": "ink"}).json()["results"] == []   # nothing embedded with this model


def test_query_without_known_words_returns_empty(client, tmp_path, sem):
    upload(client, S.write_pdf(spec_with(["Printer toner"], 1), tmp_path / "a.pdf")); worker.run_all()
    assert client.get("/api/search/semantic", params={"q": "!!! ???"}).json()["results"] == []
    assert client.get("/api/search/semantic", params={"q": ""}).status_code == 422


def test_semantic_endpoints_respect_api_key(client, monkeypatch):
    monkeypatch.setenv("DOCINTEL_API_KEY", "k")
    assert client.get("/api/search/semantic", params={"q": "x"}).status_code == 401
    assert client.get("/api/semantic/status").status_code == 401


def _find_glove():
    """A prepared GloVe directory, wherever the user put it (env override, eval/models, or the default data dir)."""
    import os
    from pathlib import Path
    here = Path(__file__).resolve()
    cands = [Path(os.environ["DOCINTEL_MODELS_DIR"]) / "glove50"] if os.environ.get("DOCINTEL_MODELS_DIR") else []
    cands += [here.parents[2] / "eval" / "models" / "glove50", here.parents[1] / "data" / "models" / "glove50"]
    return next((c for c in cands if (c / "vectors.npy").exists() and (c / "vocab.txt").exists()), None)


@pytest.mark.skipif(_find_glove() is None, reason="GloVe not prepared (python -m app.semantic download-glove)")
def test_real_glove_ranks_related_phrases_higher():
    from app.semantic import GloveEmbedder
    e = GloveEmbedder(_find_glove(), 100000)
    q, good, bad = e.embed(["protective hand wear", "safety gloves", "server rack"])
    assert float(q @ good) > float(q @ bad) + 0.2


def test_hybrid_is_default_and_reports_why_a_document_matched(client, tmp_path, sem):
    upload(client, S.write_pdf(spec_with(["Printer toner"], 1, "Zephyr Aerospace"), tmp_path / "print.pdf"),
           S.write_pdf(spec_with(["Safety gloves"], 2, "Borealis Foods"), tmp_path / "safety.pdf"))
    worker.run_all()
    r = client.get("/api/search/semantic", params={"q": "ink cartridge"}).json()["results"]
    assert r[0]["filename"] == "print.pdf" and r[0]["mode"] == "hybrid" and "semantic" in r[0]["sources"]
    r = client.get("/api/search/semantic", params={"q": "safety"}).json()["results"]
    assert r[0]["filename"] == "safety.pdf" and set(r[0]["sources"]) == {"semantic", "keyword"}
    assert client.get("/api/search/semantic", params={"q": "ink", "mode": "bogus"}).status_code == 422
    sem_only = client.get("/api/search/semantic", params={"q": "safety", "mode": "semantic"}).json()["results"][0]
    assert sem_only["sources"] == ["semantic"] and sem_only["mode"] == "semantic"


def test_hybrid_falls_back_to_keyword_when_the_model_cannot_represent_the_query(client, tmp_path, monkeypatch):
    class Blind(FakeEmbedder):
        def embed(self, texts): return np.zeros((len(texts), self.dim), dtype=np.float32)
    monkeypatch.setenv("DOCINTEL_SEMANTIC", "glove"); semantic.reset_embedder(); monkeypatch.setattr(semantic, "_embedder", Blind())
    s = spec_with(["Printer toner"], 1)
    upload(client, S.write_pdf(s, tmp_path / "a.pdf")); worker.run_all()
    r = client.get("/api/search/semantic", params={"q": s.number}).json()["results"]          # an identifier, found lexically
    assert r and r[0]["filename"] == "a.pdf" and r[0]["sources"] == ["keyword"] and "<mark>" in r[0]["snippets"][0]["snippet"]
    assert client.get("/api/search/semantic", params={"q": s.number, "mode": "semantic"}).json()["results"] == []
    semantic.reset_embedder()


def test_hybrid_finds_documents_that_have_no_embeddings_yet(client, tmp_path, monkeypatch):
    upload(client, S.write_pdf(spec_with(["Printer toner"], 1), tmp_path / "old.pdf")); worker.run_all()      # processed while disabled
    monkeypatch.setenv("DOCINTEL_SEMANTIC", "glove"); semantic.reset_embedder(); monkeypatch.setattr(semantic, "_embedder", FakeEmbedder())
    r = client.get("/api/search/semantic", params={"q": "toner"}).json()["results"]
    assert [x["filename"] for x in r] == ["old.pdf"] and r[0]["sources"] == ["keyword"]
    semantic.reset_embedder()


def test_index_is_extended_incrementally_and_rebuilt_after_deletions(client, tmp_path, sem):
    upload(client, S.write_pdf(spec_with(["Printer toner"], 1), tmp_path / "a.pdf")); worker.run_all()
    semantic.search("ink", limit=5)
    builds = semantic._full_builds
    upload(client, S.write_pdf(spec_with(["Safety gloves"], 2), tmp_path / "b.pdf")); worker.run_all()
    names = [client.get(f"/api/documents/{r['document_id']}").json()["filename"] for r in semantic.search("hand gloves", limit=5)]
    assert names[0] == "b.pdf"
    assert semantic._full_builds == builds                              # the new document was appended, not a full rebuild
    client.post("/api/documents/1/reprocess"); worker.run_all()          # deletes + re-inserts chunks of doc 1
    assert [r["document_id"] for r in semantic.search("ink", limit=5)][0] == 1
    assert semantic._full_builds == builds + 1                          # deletion forces exactly one rebuild
    idx = semantic.get_index("fake-64", 64)
    assert len(idx.meta) == idx.matrix.shape[0] == len({m[0] for m in idx.meta})
