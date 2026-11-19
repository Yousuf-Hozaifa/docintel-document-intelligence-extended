
"""Optional semantic search: chunk page text, embed with a local model, brute-force cosine search.

Backends (DOCINTEL_SEMANTIC): off (default) | glove (averaged GloVe-50 word vectors, numpy only)
                              | sbert (sentence-transformers; optional dependency)
Keyword search never depends on this module. Embedding failures never fail document processing.

Scoring note: cosine scores of averaged word vectors are compressed (most are high); use the RANKING,
not an absolute threshold.

Tenant isolation:
- API search functions accept an optional tenant_id.
- Tenant filtering happens before semantic candidate ranking.
- Keyword search filters documents in SQL.
- Status and reindex operations can be scoped to a tenant.
- Omitting tenant_id preserves the original internal/global behavior.
"""

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
STOP = set(
    "a an the and or of to in on for with by at from is are was were be "
    "as it this that these those".split()
)
GLOVE_URL = (
    "https://github.com/RaRe-Technologies/gensim-data/releases/download/"
    "glove-wiki-gigaword-50/glove-wiki-gigaword-50.gz"
)
GLOVE_FULL_VOCAB = 400_000


class SemanticDisabled(RuntimeError):
    pass


# ------------------------------------------------------------------ embedders
class GloveEmbedder:
    """Sentence vector = SIF-weighted mean of GloVe word vectors.

    Frequent words weigh less. Out-of-vocabulary tokens (rare words, most
    identifiers) are ignored. Static word vectors provide a lightweight
    baseline without sentence context.
    """

    dim = 50

    def __init__(self, directory: Path, max_vocab: int):
        vocab_file = directory / "vocab.txt"
        vec_file = directory / "vectors.npy"

        if not (vocab_file.exists() and vec_file.exists()):
            raise SemanticDisabled(
                f"GloVe files not prepared in {directory}. "
                "Run: python -m app.semantic download-glove"
            )

        words = vocab_file.read_text(
            encoding="utf-8"
        ).split("\n")[:max_vocab]

        self.index = {word: i for i, word in enumerate(words)}
        self.vectors = np.load(vec_file, mmap_mode="r")[:len(words)]

        h = math.log(GLOVE_FULL_VOCAB) + 0.577
        ranks = np.arange(len(words), dtype=np.float64)
        p = 1.0 / ((ranks + 1) * h)

        self.weights = (
            1e-3 / (1e-3 + p)
        ).astype(np.float32)

        self.name = f"glove-50d-v{len(words)}"

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros(
            (len(texts), self.dim),
            dtype=np.float32,
        )

        for row, text in enumerate(texts):
            ids = [
                self.index[word]
                for word in TOKEN_RE.findall(text.lower())
                if word not in STOP and word in self.index
            ]

            if ids:
                vectors = (
                    np.asarray(self.vectors[ids])
                    * self.weights[ids, None]
                )

                vector = vectors.sum(axis=0)
                norm = np.linalg.norm(vector)

                if norm > 0:
                    out[row] = vector / norm

        return out


class SbertEmbedder:
    """sentence-transformers adapter, e.g. all-MiniLM-L6-v2.

    This optional backend should be benchmarked with eval/bench_semantic.py
    before being relied on in production.
    """

    def __init__(self, model_name: str):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            raise SemanticDisabled(
                "pip install sentence-transformers to use "
                "DOCINTEL_SEMANTIC=sbert"
            ) from error

        self.model = SentenceTransformer(
            model_name,
            device="cpu",
        )
        self.dim = int(
            self.model.get_sentence_embedding_dimension()
        )
        self.name = f"sbert:{model_name}"
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> np.ndarray:
        with self._lock:
            return np.asarray(
                self.model.encode(
                    texts,
                    batch_size=16,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                ),
                dtype=np.float32,
            )


_embedder = None
_embedder_lock = threading.Lock()


def enabled() -> bool:
    return get_settings().semantic_backend not in (
        "off",
        "",
        "none",
    )


def get_embedder():
    """Lazily load the configured embedder."""
    global _embedder

    settings = get_settings()

    if not enabled():
        return None

    with _embedder_lock:
        if _embedder is None:
            if settings.semantic_backend == "glove":
                _embedder = GloveEmbedder(
                    settings.models_dir / "glove50",
                    settings.glove_vocab,
                )

            elif settings.semantic_backend == "sbert":
                _embedder = SbertEmbedder(
                    settings.sbert_model
                )

            else:
                raise SemanticDisabled(
                    "Unknown DOCINTEL_SEMANTIC backend "
                    f"'{settings.semantic_backend}'"
                )

        return _embedder


def reset_embedder() -> None:
    """Reset the loaded model and in-memory index."""
    global _embedder, _index

    with _embedder_lock:
        _embedder = None

    with _index_lock:
        _index = None


# ------------------------------------------------------------------ chunking
def chunk_text(text: str, max_chars: int) -> list[str]:
    """Group consecutive lines into chunks of approximately max_chars."""
    chunks = []
    current = []
    current_length = 0

    for line in (
        re.sub(r"\s+", " ", line).strip()
        for line in text.splitlines()
    ):
        if not line:
            continue

        if current and current_length + len(line) > max_chars:
            chunks.append(" ".join(current))
            current = []
            current_length = 0

        current.append(line)
        current_length += len(line) + 1

    if current:
        chunks.append(" ".join(current))

    return chunks


# ------------------------------------------------------------------ storage
PAGE_BATCH = 50


def embed_document(
    doc_id: int,
    generation_id: int | None = None,
) -> int:
    """Build embeddings for one document generation."""
    embedder = get_embedder()

    if embedder is None:
        return 0

    settings = get_settings()

    with connect() as db:
        if generation_id is None:
            doc = db.execute(
                """
                SELECT active_generation
                FROM documents
                WHERE id = ?
                """,
                (doc_id,),
            ).fetchone()

            if doc is None:
                return 0

            generation_id = doc["active_generation"]

        db.execute(
            """
            DELETE FROM chunks
            WHERE document_id = ?
              AND generation_id = ?
            """,
            (doc_id, generation_id),
        )

    last_page = 0
    written = 0

    while True:
        with connect() as db:
            pages = db.execute(
                """
                SELECT page_no, text
                FROM pages
                WHERE document_id = ?
                  AND generation_id = ?
                  AND page_no > ?
                  AND text != ''
                ORDER BY page_no
                LIMIT ?
                """,
                (
                    doc_id,
                    generation_id,
                    last_page,
                    PAGE_BATCH,
                ),
            ).fetchall()

        if not pages:
            break

        last_page = pages[-1]["page_no"]

        rows = [
            (page["page_no"], index, chunk)
            for page in pages
            for index, chunk in enumerate(
                chunk_text(
                    page["text"],
                    settings.chunk_chars,
                )
            )
        ]

        if not rows:
            continue

        vectors = embedder.embed(
            [row[2] for row in rows]
        )

        with connect() as db:
            db.executemany(
                """
                INSERT INTO chunks
                    (
                        document_id,
                        generation_id,
                        page_no,
                        chunk_idx,
                        text,
                        model,
                        dim,
                        embedding
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        doc_id,
                        generation_id,
                        page_no,
                        chunk_index,
                        text,
                        embedder.name,
                        embedder.dim,
                        vector.astype(np.float32).tobytes(),
                    )
                    for (page_no, chunk_index, text), vector
                    in zip(rows, vectors)
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

    started = time.perf_counter()

    try:
        count = embed_document(
            doc_id,
            generation_id,
        )

        detail = (
            f"chunks={count} "
            f"seconds={time.perf_counter() - started:.2f}"
        )
        event = "embedded"

    except Exception as error:  # noqa: BLE001
        detail = (
            f"{type(error).__name__}: {str(error)[:200]}"
        )
        event = "embedding_failed"

    try:
        with connect() as db:
            add_event(
                db,
                doc_id,
                event,
                detail,
            )
    except Exception:
        # Event logging is also best-effort.
        pass


# ------------------------------------------------------------------ in-memory index
class _Index:
    def __init__(self, key, matrix, meta):
        self.key = key
        self.matrix = matrix
        # meta: list of (chunk_id, document_id, page_no)
        self.meta = meta


_index: _Index | None = None
_index_lock = threading.Lock()

_full_builds = 0


def _read_chunks(
    db,
    model: str,
    dim: int,
    after_id: int,
):
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

    matrix = np.empty(
        (len(rows), dim),
        dtype=np.float32,
    )

    for index, row in enumerate(rows):
        matrix[index] = np.frombuffer(
            row["embedding"],
            dtype=np.float32,
        )

    metadata = [
        (
            row["id"],
            row["document_id"],
            row["page_no"],
        )
        for row in rows
    ]

    return matrix, metadata


def get_index(model: str, dim: int) -> _Index:
    """Build an index containing only active-generation chunks.

    Append new chunks incrementally when the previous active chunk set
    is unchanged. Rebuild when generations switch or chunks are deleted.
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
                old_ids = [
                    item[0]
                    for item in old.meta
                ]

                active_old_ids = [
                    row[0]
                    for row in db.execute(
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

                if (
                    active_old_ids == old_ids
                    and len(old.meta) + new_rows == count
                ):
                    added_matrix, added_meta = _read_chunks(
                        db,
                        model,
                        dim,
                        old.key[2],
                    )

                    _index = _Index(
                        key,
                        (
                            np.vstack([
                                old.matrix,
                                added_matrix,
                            ])
                            if len(added_meta)
                            else old.matrix
                        ),
                        old.meta + added_meta,
                    )

                    return _index

            matrix, metadata = _read_chunks(
                db,
                model,
                dim,
                0,
            )

            _full_builds += 1
            _index = _Index(
                key,
                matrix,
                metadata,
            )

            return _index


# ------------------------------------------------------------------ search
def search(
    query: str,
    limit: int = 10,
    candidates: int = 300,
    tenant_id: int | None = None,
) -> list[dict]:
    """Rank documents by their best-matching chunk.

    When tenant_id is supplied, only that tenant's document chunks are
    considered before candidate selection and ranking.
    """
    if limit <= 0 or candidates <= 0:
        return []

    embedder = get_embedder()

    if embedder is None:
        raise SemanticDisabled(
            "Semantic search is disabled "
            "(set DOCINTEL_SEMANTIC=glove)"
        )

    query_vector = embedder.embed([query])[0]

    if not np.any(query_vector):
        return []

    index = get_index(
        embedder.name,
        embedder.dim,
    )

    if not len(index.meta):
        return []

    allowed_documents = None

    if tenant_id is not None:
        with connect() as db:
            allowed_documents = {
                row["id"]
                for row in db.execute(
                    """
                    SELECT id
                    FROM documents
                    WHERE tenant_id = ?
                    """,
                    (tenant_id,),
                )
            }

        if not allowed_documents:
            return []

    # Filter before scoring and candidate selection.
    # This prevents other tenants' chunks from occupying the
    # candidate limit or influencing this tenant's ranking.
    positions = [
        position
        for position, metadata in enumerate(index.meta)
        if (
            allowed_documents is None
            or metadata[1] in allowed_documents
        )
    ]

    if not positions:
        return []

    tenant_matrix = index.matrix[positions]
    scores = tenant_matrix @ query_vector

    candidate_count = min(
        candidates,
        len(scores),
    )

    top_local = np.argpartition(
        -scores,
        candidate_count - 1,
    )[:candidate_count]

    top_local = top_local[
        np.argsort(-scores[top_local])
    ]

    best: dict[int, tuple[float, int]] = {}

    for local_position in top_local:
        original_position = positions[
            int(local_position)
        ]

        _, document_id, _ = index.meta[
            original_position
        ]

        if document_id not in best:
            best[document_id] = (
                float(scores[local_position]),
                original_position,
            )

    ranked = sorted(
        best.items(),
        key=lambda item: -item[1][0],
    )[:limit]

    chunk_ids = [
        index.meta[original_position][0]
        for _, (_, original_position) in ranked
    ]

    if not chunk_ids:
        return []

    placeholders = ",".join(
        "?" for _ in chunk_ids
    )

    with connect() as db:
        if tenant_id is None:
            rows = db.execute(
                f"""
                SELECT c.id, c.text
                FROM chunks c
                WHERE c.id IN ({placeholders})
                """,
                chunk_ids,
            )
        else:
            rows = db.execute(
                f"""
                SELECT c.id, c.text
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.id IN ({placeholders})
                  AND d.tenant_id = ?
                """,
                (*chunk_ids, tenant_id),
            )

        texts = {
            row["id"]: row["text"]
            for row in rows
        }

    results = []

    for document_id, (score, original_position) in ranked:
        chunk_id, _, page_no = index.meta[
            original_position
        ]

        # Apply a second ownership check when reading chunk text.
        if chunk_id not in texts:
            continue

        results.append({
            "document_id": document_id,
            "page_no": page_no,
            "chunk_id": chunk_id,
            "score": round(score, 4),
            "text": texts[chunk_id],
        })

    return results


def keyword_docs(
    query: str,
    limit: int = 100,
    tenant_id: int | None = None,
) -> list[tuple[int, int, str]]:
    """Search active-generation pages using FTS5.

    When tenant_id is supplied, results are restricted in SQL.
    """
    if limit <= 0:
        return []

    tokens = [
        token
        for token in TOKEN_RE.findall(query.lower())
        if token not in STOP
    ]

    if not tokens:
        return []

    sql = """
        SELECT
            f.document_id,
            f.page_no,
            snippet(
                page_fts,
                0,
                '<mark>',
                '</mark>',
                ' … ',
                14
            ) AS sn
        FROM page_fts f
        JOIN documents d ON d.id = f.document_id
        WHERE page_fts MATCH ?
          AND f.generation_id = d.active_generation
    """

    args: list = [
        " OR ".join(
            f'"{token}"'
            for token in tokens
        )
    ]

    if tenant_id is not None:
        sql += " AND d.tenant_id = ?"
        args.append(tenant_id)

    sql += """
        ORDER BY rank
        LIMIT ?
    """

    args.append(limit * 3)

    with connect() as db:
        rows = db.execute(
            sql,
            args,
        ).fetchall()

    seen = set()
    output = []

    for row in rows:
        document_id = row["document_id"]

        if document_id not in seen:
            seen.add(document_id)
            output.append((
                document_id,
                row["page_no"],
                row["sn"],
            ))

    return output[:limit]


def hybrid_search(
    query: str,
    limit: int = 10,
    k: int = 60,
    tenant_id: int | None = None,
) -> list[dict]:
    """Reciprocal-rank fusion of semantic and keyword rankings.

    Both ranking sources are restricted to the supplied tenant.
    """
    semantic_results = search(
        query,
        limit=100,
        tenant_id=tenant_id,
    )

    keyword_results = keyword_docs(
        query,
        limit=100,
        tenant_id=tenant_id,
    )

    scores: dict[int, float] = {}
    info: dict[int, dict] = {}

    for rank, hit in enumerate(semantic_results):
        document_id = hit["document_id"]

        scores[document_id] = (
            scores.get(document_id, 0)
            + 1 / (k + rank + 1)
        )

        info[document_id] = {
            **hit,
            "sources": ["semantic"],
        }

    for rank, (document_id, page_no, snippet_text) in enumerate(
        keyword_results
    ):
        scores[document_id] = (
            scores.get(document_id, 0)
            + 1 / (k + rank + 1)
        )

        if document_id in info:
            info[document_id]["sources"].append(
                "keyword"
            )
        else:
            info[document_id] = {
                "document_id": document_id,
                "page_no": page_no,
                "chunk_id": None,
                "text": snippet_text,
                "sources": ["keyword"],
            }

    ranked = sorted(
        scores,
        key=lambda document_id: -scores[document_id],
    )[:limit]

    return [
        {
            **info[document_id],
            "score": round(scores[document_id], 5),
        }
        for document_id in ranked
    ]


# ------------------------------------------------------------------ status / reindex
_reindex_state = {
    "running": False,
    "done": 0,
    "total": 0,
    "error": None,
}

_reindex_tenant_id: int | None = None
_reindex_lock = threading.Lock()


def status(tenant_id: int | None = None) -> dict:
    """Return semantic status, optionally scoped to a tenant."""
    settings = get_settings()

    out = {
        "backend": settings.semantic_backend,
        "enabled": enabled(),
    }

    with _reindex_lock:
        if (
            tenant_id is None
            or _reindex_tenant_id == tenant_id
        ):
            out["reindex"] = dict(_reindex_state)
        else:
            # Do not expose another tenant's reindex progress or errors.
            out["reindex"] = {
                "running": False,
                "done": 0,
                "total": 0,
                "error": None,
            }

    if not enabled():
        return out

    with connect() as db:
        if tenant_id is None:
            document_sql = """
                SELECT COUNT(*)
                FROM documents
                WHERE status IN ('completed', 'partial')
            """
            document_args = ()
        else:
            document_sql = """
                SELECT COUNT(*)
                FROM documents
                WHERE status IN ('completed', 'partial')
                  AND tenant_id = ?
            """
            document_args = (tenant_id,)

        out["documents_total"] = db.execute(
            document_sql,
            document_args,
        ).fetchone()[0]

        if _embedder is not None:
            model_name = _embedder.name

            if tenant_id is None:
                chunk_filter = ""
                chunk_args = (model_name,)
            else:
                chunk_filter = " AND d.tenant_id = ?"
                chunk_args = (
                    model_name,
                    tenant_id,
                )

            out["chunks"] = db.execute(
                """
                SELECT COUNT(*)
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.generation_id = d.active_generation
                  AND c.model = ?
                """ + chunk_filter,
                chunk_args,
            ).fetchone()[0]

            out["documents_embedded"] = db.execute(
                """
                SELECT COUNT(DISTINCT c.document_id)
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.generation_id = d.active_generation
                  AND c.model = ?
                """ + chunk_filter,
                chunk_args,
            ).fetchone()[0]

        else:
            out["chunks"] = 0
            out["documents_embedded"] = 0

    out["model_loaded"] = _embedder is not None
    out["model"] = (
        _embedder.name
        if _embedder is not None
        else None
    )

    return out


def start_reindex(
    tenant_id: int | None = None,
) -> bool:
    """Embed finished documents missing chunks for the current model.

    If tenant_id is supplied, only that tenant's documents are reindexed.
    A single reindex operation runs at a time.
    """
    global _reindex_tenant_id

    if not enabled():
        raise SemanticDisabled(
            "Semantic search is disabled "
            "(set DOCINTEL_SEMANTIC=glove)"
        )

    embedder = get_embedder()
    model_name = embedder.name

    with _reindex_lock:
        if _reindex_state["running"]:
            return False

        query = """
            SELECT d.id, d.active_generation
            FROM documents d
            WHERE d.status IN ('completed', 'partial')
        """

        args: list = []

        if tenant_id is not None:
            query += " AND d.tenant_id = ?"
            args.append(tenant_id)

        query += """
            AND NOT EXISTS (
                SELECT 1
                FROM chunks c
                WHERE c.document_id = d.id
                  AND c.generation_id = d.active_generation
                  AND c.model = ?
            )
            ORDER BY d.id
        """

        args.append(model_name)

        with connect() as db:
            rows = db.execute(
                query,
                args,
            ).fetchall()

        targets = [
            (
                row["id"],
                row["active_generation"],
            )
            for row in rows
        ]

        _reindex_tenant_id = tenant_id

        _reindex_state.update(
            running=True,
            done=0,
            total=len(targets),
            error=None,
        )

    def run():
        try:
            for document_id, generation_id in targets:
                embed_document(
                    document_id,
                    generation_id,
                )

                with _reindex_lock:
                    _reindex_state["done"] += 1

        except Exception as error:  # noqa: BLE001
            with _reindex_lock:
                _reindex_state["error"] = (
                    f"{type(error).__name__}: {error}"
                )

        finally:
            with _reindex_lock:
                _reindex_state["running"] = False

    threading.Thread(
        target=run,
        name="semantic-reindex",
        daemon=True,
    ).start()

    return True


# ------------------------------------------------------------------ GloVe preparation (CLI)
def prepare_glove(
    gz_path: Path,
    out_dir: Path,
) -> None:
    out_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    words = []
    rows = []

    started = time.perf_counter()

    with gzip.open(
        gz_path,
        "rt",
        encoding="utf-8",
    ) as file:
        first = file.readline().split()

        dim = (
            int(first[1])
            if len(first) == 2
            else 50
        )

        if len(first) != 2:
            file.seek(0)

        for line in file:
            parts = line.rstrip("\n").split(" ")

            if (
                len(parts) != dim + 1
                or "\n" in parts[0]
            ):
                continue

            words.append(parts[0])
            rows.append(parts[1:])

    matrix = np.array(
        rows,
        dtype=np.float32,
    )

    np.save(
        out_dir / "vectors.npy",
        matrix,
    )

    (out_dir / "vocab.txt").write_text(
        "\n".join(words),
        encoding="utf-8",
    )

    print(
        f"prepared {len(words)} words x {dim} dims "
        f"in {time.perf_counter() - started:.1f}s "
        f"-> {out_dir}"
    )


def download_glove(out_dir: Path) -> None:
    gz_path = (
        out_dir.parent
        / "glove-wiki-gigaword-50.gz"
    )

    gz_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not gz_path.exists():
        print(
            f"downloading {GLOVE_URL} (~66 MB)"
        )

        urllib.request.urlretrieve(
            GLOVE_URL,
            gz_path,
        )

    prepare_glove(
        gz_path,
        out_dir,
    )


if __name__ == "__main__":
    command = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "status"
    )

    settings = get_settings()

    if command == "download-glove":
        download_glove(
            settings.models_dir / "glove50"
        )

    elif command == "prepare-glove":
        prepare_glove(
            Path(sys.argv[2]),
            settings.models_dir / "glove50",
        )

    elif command == "status":
        from .db import init_db

        init_db()
        print(status())

    else:
        print(
            "usage: python -m app.semantic "
            "[status|download-glove|prepare-glove <gz>]"
        )
