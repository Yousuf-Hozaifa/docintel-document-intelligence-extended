"""Optional semantic search: chunk page text, embed with a local model, brute-force cosine search.

Backends (DOCINTEL_SEMANTIC): off (default) | glove (averaged GloVe-50 word vectors, numpy only)
                              | sbert (sentence-transformers; optional dependency)
Keyword search never depends on this module. Embedding failures never fail document processing.

Scoring note: cosine scores of averaged word vectors are compressed (most are high); use the RANKING,
not an absolute threshold."""
import gzip
import math
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

from .config import get_settings
from .db import add_event, connect

TOKEN_RE = re.compile(r"[a-z0-9]+")
STOP = set("a an the and or of to in on for with by at from is are was were be as it this that these those".split())
GLOVE_URL = "https://github.com/RaRe-Technologies/gensim-data/releases/download/glove-wiki-gigaword-50/glove-wiki-gigaword-50.gz"
GLOVE_FULL_VOCAB = 400_000


class SemanticDisabled(RuntimeError):
    pass


# ------------------------------------------------------------------ embedders
class GloveEmbedder:
    """Sentence vector = SIF-weighted mean of GloVe word vectors (frequent words weigh less). Out-of-vocabulary
    tokens (rare words, most identifiers) are ignored. Static word vectors: synonyms land close together,
    but there is no sentence context, so this is a lightweight baseline rather than a transformer."""
    dim = 50

    def __init__(self, directory: Path, max_vocab: int):
        vocab_file, vec_file = directory / "vocab.txt", directory / "vectors.npy"
        if not (vocab_file.exists() and vec_file.exists()):
            raise SemanticDisabled(f"GloVe files not prepared in {directory}. Run: python -m app.semantic download-glove")
        words = vocab_file.read_text(encoding="utf-8").split("\n")[:max_vocab]
        self.index = {w: i for i, w in enumerate(words)}
        self.vectors = np.load(vec_file, mmap_mode="r")[: len(words)]
        h = math.log(GLOVE_FULL_VOCAB) + 0.577
        ranks = np.arange(len(words), dtype=np.float64)
        p = 1.0 / ((ranks + 1) * h)
        self.weights = (1e-3 / (1e-3 + p)).astype(np.float32)
        self.name = f"glove-50d-v{len(words)}"

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for r, t in enumerate(texts):
            ids = [self.index[w] for w in TOKEN_RE.findall(t.lower()) if w not in STOP and w in self.index]
            if ids:
                v = (np.asarray(self.vectors[ids]) * self.weights[ids, None]).sum(axis=0)
                n = np.linalg.norm(v)
                if n > 0:
                    out[r] = v / n
        return out


class SbertEmbedder:
    """sentence-transformers adapter (e.g. all-MiniLM-L6-v2). NOT exercised in the development sandbox
    (model hosts were unreachable): benchmark it with eval/bench_semantic.py before relying on it."""

    def __init__(self, model_name: str):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise SemanticDisabled("pip install sentence-transformers to use DOCINTEL_SEMANTIC=sbert") from e
        self.model = SentenceTransformer(model_name, device="cpu")
        self.dim = int(self.model.get_sentence_embedding_dimension())
        self.name = f"sbert:{model_name}"
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> np.ndarray:
        with self._lock:
            return np.asarray(self.model.encode(texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False), dtype=np.float32)


_embedder = None
_embedder_lock = threading.Lock()


def enabled() -> bool:
    return get_settings().semantic_backend not in ("off", "", "none")


def get_embedder():
    """Lazily load the configured embedder (the model costs no memory until first use). None when disabled."""
    global _embedder
    s = get_settings()
    if not enabled():
        return None
    with _embedder_lock:
        if _embedder is None:
            if s.semantic_backend == "glove":
                _embedder = GloveEmbedder(s.models_dir / "glove50", s.glove_vocab)
            elif s.semantic_backend == "sbert":
                _embedder = SbertEmbedder(s.sbert_model)
            else:
                raise SemanticDisabled(f"Unknown DOCINTEL_SEMANTIC backend '{s.semantic_backend}'")
        return _embedder


def reset_embedder() -> None:
    global _embedder, _index
    with _embedder_lock:
        _embedder = None
    _index = None


# ------------------------------------------------------------------ chunking
def chunk_text(text: str, max_chars: int) -> list[str]:
    """Group consecutive lines into chunks of about max_chars (a single longer line becomes its own chunk)."""
    chunks, cur, n = [], [], 0
    for line in (re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()):
        if not line:
            continue
        if cur and n + len(line) > max_chars:
            chunks.append(" ".join(cur)); cur, n = [], 0
        cur.append(line); n += len(line) + 1
    if cur:
        chunks.append(" ".join(cur))
    return chunks


# ------------------------------------------------------------------ storage
PAGE_BATCH = 50


def embed_document(doc_id: int, generation_id: int | None = None) -> int:
    """Build embeddings for one document generation without modifying other generations."""
    emb = get_embedder()
    if emb is None:
        return 0

    s = get_settings()

    with connect() as db:
        if generation_id is None:
            doc = db.execute(
                "SELECT active_generation FROM documents WHERE id=?",
                (doc_id,),
            ).fetchone()
            if doc is None:
                return 0
            generation_id = doc["active_generation"]

        db.execute(
            "DELETE FROM chunks WHERE document_id=? AND generation_id=?",
            (doc_id, generation_id),
        )

    last, written = 0, 0

    while True:
        with connect() as db:
            pages = db.execute(
                "SELECT page_no, text FROM pages "
                "WHERE document_id=? AND generation_id=? "
                "AND page_no>? AND text!='' "
                "ORDER BY page_no LIMIT ?",
                (doc_id, generation_id, last, PAGE_BATCH),
            ).fetchall()

        if not pages:
            break

        last = pages[-1]["page_no"]

        rows = [
            (p["page_no"], i, c)
            for p in pages
            for i, c in enumerate(chunk_text(p["text"], s.chunk_chars))
        ]

        if not rows:
            continue

        vecs = emb.embed([r[2] for r in rows])

        with connect() as db:
            db.executemany(
                "INSERT INTO chunks "
                "(document_id,generation_id,page_no,chunk_idx,text,model,dim,embedding) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        doc_id,
                        generation_id,
                        pn,
                        ci,
                        txt,
                        emb.name,
                        emb.dim,
                        v.astype(np.float32).tobytes(),
                    )
                    for (pn, ci, txt), v in zip(rows, vecs)
                ],
            )

        written += len(rows)

    return written



def embed_after_processing(
    doc_id: int,
    generation_id: int | None = None,
) -> None:
    """Best-effort indexing; semantic failures never fail document processing."""
    if not enabled():
        return

    t0 = time.perf_counter()

    try:
        n = embed_document(doc_id, generation_id)
        detail = f"chunks={n} seconds={time.perf_counter() - t0:.2f}"
        event = "embedded"
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {str(e)[:200]}"
        event = "embedding_failed"

    try:
        with connect() as db:
            add_event(db, doc_id, event, detail)
    except Exception:
        # Event logging is also best-effort and must not fail processing.
        pass

# ------------------------------------------------------------------ in-memory index (brute force)
class _Index:
    def __init__(self, key, matrix, meta):
        self.key, self.matrix, self.meta = key, matrix, meta  # meta: list of (chunk_id, document_id, page_no)


_index: _Index | None = None
_index_lock = threading.Lock()


_full_builds = 0   # counts complete rebuilds (observable in tests / benchmarks)


def _read_chunks(db, model: str, dim: int, after_id: int):
    rows = db.execute(
        """
        SELECT c.id, c.document_id, c.page_no, c.embedding
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE c.model = ?
          AND c.id > ?
          AND c.generation_id = d.active_generation
        ORDER BY c.id
        """,
        (model, after_id),
    ).fetchall()

    matrix = np.empty((len(rows), dim), dtype=np.float32)

    for i, r in enumerate(rows):
        matrix[i] = np.frombuffer(r["embedding"], dtype=np.float32)

    return matrix, [
        (r["id"], r["document_id"], r["page_no"])
        for r in rows
    ]



def get_index(model: str, dim: int) -> _Index:
    """Build an index containing only active-generation chunks.

    Append newly added chunks incrementally when the previous active
    chunk set is unchanged. Rebuild when generations switch or chunks
    are deleted.
    """
    global _index, _full_builds

    with connect() as db:
        count, max_id = db.execute(
            """
            SELECT COUNT(*), COALESCE(MAX(c.id), 0)
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.model = ?
              AND c.generation_id = d.active_generation
            """,
            (model,),
        ).fetchone()

        key = (model, count, max_id)

        with _index_lock:
            old = _index

            # An unchanged empty index is safe to reuse.
            if (
                old is not None
                and old.key == key
                and not old.meta
                and count == 0
            ):
                return old

            if (
                old is not None
                and old.key[0] == model
                and old.key[2] <= max_id
                and len(old.meta) > 0
            ):
                old_ids = [item[0] for item in old.meta]

                # Verify that every previously indexed chunk is
                # still part of its document's active generation.
                active_old_ids = [
                    r[0]
                    for r in db.execute(
                        """
                        SELECT c.id
                        FROM chunks c
                        JOIN documents d ON d.id = c.document_id
                        WHERE c.model = ?
                          AND c.id <= ?
                          AND c.generation_id = d.active_generation
                        ORDER BY c.id
                        """,
                        (model, old.key[2]),
                    ).fetchall()
                ]

                new_rows = db.execute(
                    """
                    SELECT COUNT(*)
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE c.model = ?
                      AND c.id > ?
                      AND c.generation_id = d.active_generation
                    """,
                    (model, old.key[2]),
                ).fetchone()[0]

                # Incremental update is safe only if no previously
                # indexed active chunks were removed or replaced.
                if (
                    active_old_ids == old_ids
                    and len(old.meta) + new_rows == count
                ):
                    add, meta = _read_chunks(
                        db, model, dim, old.key[2]
                    )

                    _index = _Index(
                        key,
                        np.vstack([old.matrix, add])
                        if len(meta)
                        else old.matrix,
                        old.meta + meta,
                    )

                    return _index

            # Rebuild when the active generation membership changes,
            # chunks are deleted, or the model changes.
            matrix, meta = _read_chunks(db, model, dim, 0)

            _full_builds += 1
            _index = _Index(key, matrix, meta)

            return _index

def search(query: str, limit: int = 10, candidates: int = 300) -> list[dict]:
    """Rank documents by their best-matching chunk. Returns [{document_id, page_no, chunk_id, score, text}]."""
    emb = get_embedder()
    if emb is None:
        raise SemanticDisabled("Semantic search is disabled (set DOCINTEL_SEMANTIC=glove)")
    q = emb.embed([query])[0]
    if not np.any(q):
        return []
    idx = get_index(emb.name, emb.dim)
    if not len(idx.meta):
        return []
    scores = idx.matrix @ q
    k = min(candidates, len(scores))
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]
    best: dict[int, tuple[float, int]] = {}
    for i in top:
        _, doc_id, _ = idx.meta[i]
        if doc_id not in best:
            best[doc_id] = (float(scores[i]), int(i))
    ranked = sorted(best.items(), key=lambda kv: -kv[1][0])[:limit]
    ids = [idx.meta[i][0] for _, (_, i) in ranked]
    with connect() as db:
        texts = {r["id"]: r["text"] for r in db.execute(f"SELECT id, text FROM chunks WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    return [{"document_id": d, "page_no": idx.meta[i][2], "chunk_id": idx.meta[i][0], "score": round(sc, 4), "text": texts.get(idx.meta[i][0], "")}
            for d, (sc, i) in ranked]


def keyword_docs(query: str, limit: int = 100) -> list[tuple[int, int, str]]:
    """Search active-generation pages using FTS5."""
    toks = [
        t for t in TOKEN_RE.findall(query.lower())
        if t not in STOP
    ]

    if not toks:
        return []

    with connect() as db:
        rows = db.execute(
            """
            SELECT f.document_id, f.page_no,
                   snippet(page_fts, 0, '<mark>', '</mark>', ' … ', 14) AS sn
            FROM page_fts f
            JOIN documents d ON d.id = f.document_id
            WHERE page_fts MATCH ?
              AND f.generation_id = d.active_generation
            ORDER BY rank
            LIMIT ?
            """,
            (
                " OR ".join(f'"{t}"' for t in toks),
                limit * 3,
            ),
        ).fetchall()

    seen, out = set(), []

    for r in rows:
        if r["document_id"] not in seen:
            seen.add(r["document_id"])
            out.append((r["document_id"], r["page_no"], r["sn"]))

    return out[:limit]


def hybrid_search(query: str, limit: int = 10, k: int = 60) -> list[dict]:
    """Reciprocal-rank fusion of semantic and keyword rankings. Falls back to keyword-only for documents
    without embeddings and for queries the embedding model cannot represent (e.g. identifiers)."""
    sem = search(query, limit=100)
    kw = keyword_docs(query, 100)
    score: dict[int, float] = {}
    info: dict[int, dict] = {}
    for rank, h in enumerate(sem):
        score[h["document_id"]] = score.get(h["document_id"], 0) + 1 / (k + rank + 1)
        info[h["document_id"]] = {**h, "sources": ["semantic"]}
    for rank, (d, pn, sn) in enumerate(kw):
        score[d] = score.get(d, 0) + 1 / (k + rank + 1)
        if d in info:
            info[d]["sources"].append("keyword")
        else:
            info[d] = {"document_id": d, "page_no": pn, "chunk_id": None, "text": sn, "sources": ["keyword"]}
    ranked = sorted(score, key=lambda d: -score[d])[:limit]
    return [{**info[d], "score": round(score[d], 5)} for d in ranked]


# ------------------------------------------------------------------ status / reindex
_reindex_state = {"running": False, "done": 0, "total": 0, "error": None}



def status() -> dict:
    s = get_settings()
    out = {
        "backend": s.semantic_backend,
        "enabled": enabled(),
        "reindex": dict(_reindex_state),
    }

    if enabled():
        with connect() as db:
            out["documents_total"] = db.execute(
                """
                SELECT COUNT(*)
                FROM documents
                WHERE status IN ('completed', 'partial')
                """
            ).fetchone()[0]

            if _embedder is not None:
                model_name = _embedder.name

                out["chunks"] = db.execute(
                    """
                    SELECT COUNT(*)
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE c.generation_id = d.active_generation
                      AND c.model = ?
                    """,
                    (model_name,),
                ).fetchone()[0]

                out["documents_embedded"] = db.execute(
                    """
                    SELECT COUNT(DISTINCT c.document_id)
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE c.generation_id = d.active_generation
                      AND c.model = ?
                    """,
                    (model_name,),
                ).fetchone()[0]
            else:
                out["chunks"] = 0
                out["documents_embedded"] = 0

        out["model_loaded"] = _embedder is not None
        out["model"] = _embedder.name if _embedder else None

    return out


def start_reindex() -> bool:
    """Embed finished documents missing chunks for the current model."""
    if not enabled():
        raise SemanticDisabled(
            "Semantic search is disabled (set DOCINTEL_SEMANTIC=glove)"
        )

    embedder = get_embedder()

    if _reindex_state["running"]:
        return False

    model_name = embedder.name

    with connect() as db:
        rows = db.execute(
            """
            SELECT d.id, d.active_generation
            FROM documents d
            WHERE d.status IN ('completed', 'partial')
              AND NOT EXISTS (
                  SELECT 1
                  FROM chunks c
                  WHERE c.document_id = d.id
                    AND c.generation_id = d.active_generation
                    AND c.model = ?
              )
            ORDER BY d.id
            """,
            (model_name,),
        ).fetchall()

    targets = [
        (row["id"], row["active_generation"])
        for row in rows
    ]

    _reindex_state.update(
        running=True,
        done=0,
        total=len(targets),
        error=None,
    )

    def run():
        try:
            for doc_id, generation_id in targets:
                embed_document(doc_id, generation_id)
                _reindex_state["done"] += 1
        except Exception as error:  # noqa: BLE001
            _reindex_state["error"] = (
                f"{type(error).__name__}: {error}"
            )
        finally:
            _reindex_state["running"] = False

    threading.Thread(
        target=run,
        name="semantic-reindex",
        daemon=True,
    ).start()

    return True


# ------------------------------------------------------------------ GloVe preparation (CLI)
def prepare_glove(gz_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    words, rows = [], []
    t0 = time.perf_counter()
    with gzip.open(gz_path, "rt", encoding="utf-8") as f:
        first = f.readline().split()
        dim = int(first[1]) if len(first) == 2 else 50
        if len(first) != 2:
            f.seek(0)
        for line in f:
            parts = line.rstrip("\n").split(" ")
            if len(parts) != dim + 1 or "\n" in parts[0]:
                continue
            words.append(parts[0]); rows.append(parts[1:])
    mat = np.array(rows, dtype=np.float32)
    np.save(out_dir / "vectors.npy", mat)
    (out_dir / "vocab.txt").write_text("\n".join(words), encoding="utf-8")
    print(f"prepared {len(words)} words x {dim} dims in {time.perf_counter() - t0:.1f}s -> {out_dir}")


def download_glove(out_dir: Path) -> None:
    gz = out_dir.parent / "glove-wiki-gigaword-50.gz"
    gz.parent.mkdir(parents=True, exist_ok=True)
    if not gz.exists():
        print(f"downloading {GLOVE_URL} (~66 MB)")
        urllib.request.urlretrieve(GLOVE_URL, gz)
    prepare_glove(gz, out_dir)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    s = get_settings()
    if cmd == "download-glove":
        download_glove(s.models_dir / "glove50")
    elif cmd == "prepare-glove":
        prepare_glove(Path(sys.argv[2]), s.models_dir / "glove50")
    elif cmd == "status":
        from .db import init_db
        init_db(); print(status())
    else:
        print("usage: python -m app.semantic [status|download-glove|prepare-glove <gz>]")
