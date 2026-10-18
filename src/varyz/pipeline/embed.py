"""Local embeddings via fastembed (ONNX, CPU). Free and private — every entry
gets embedded regardless of sensitivity; vectors never leave the machine."""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

from varyz.config import Config
from varyz.db import dao

CHARS_PER_TOKEN = 4  # rough heuristic; chunking needs consistency, not precision

# Bump when chunk_text's output changes for the same input. Stored per chunk so
# that a changed splitter — or a changed chunk_tokens — re-cuts the archive
# instead of applying only to whatever is ingested next.
CHUNKER_VERSION = 3


def chunk_recipe(max_chars: int) -> str:
    return f"v{CHUNKER_VERSION}:{max_chars}"


# How far back from the budget a split will search for whitespace. A run of
# text this long with nowhere to break is a hash, a base64 blob or a URL, and
# there is no good place to cut one — so past this, cut anywhere.
_SPLIT_SEARCH_WINDOW = 200


def _split_point(paragraph: str, max_chars: int) -> int:
    """Where to cut an over-long paragraph, preferring a whitespace boundary.

    Cutting at exactly max_chars severs a word, and the damage lands in two
    places at once: the fragment becomes a junk token in the chunk's embedding,
    and — because chunks are what `recall` quotes — the evidence an agent shows
    the user opens mid-word. Markdown makes this the common case rather than
    the rare one, since a table or a bulleted list is a single "paragraph" with
    no blank line in it and routinely runs past the budget.
    """
    window = paragraph[:max_chars]
    for boundary in ("\n", " "):
        cut = window.rfind(boundary)
        # `cut > 0` is load-bearing, not defensive: rfind returns -1 when there
        # is no boundary, and with a max_chars smaller than the search window
        # that -1 satisfies the distance test and cuts nothing, which is a loop
        # that never terminates rather than a chunk that comes out wrong.
        if cut > 0 and cut >= max_chars - _SPLIT_SEARCH_WINDOW:
            return cut + 1
    return max_chars


def normalize_newlines(text: str) -> str:
    r"""One line ending, so the paragraph split below can see a paragraph.

    A CRLF document contains no `\n\n` at all — the sequence between two
    paragraphs is `\r\n\r\n` — so `split("\n\n")` found nothing and the whole
    file arrived as a single paragraph, cut by character budget instead of by
    structure. On Windows, where varyz is developed and where most of these
    files are written, that was every document. The stray `\r` also travelled
    into the embedding and into the excerpt quoted back to the user.

    Chunks are a derived artefact, so normalising here is safe: the entry body
    keeps the bytes as they were ingested.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split on paragraph boundaries, packing up to max_chars per chunk;
    split paragraphs longer than the budget at the nearest whitespace.

    A budget of zero or less is refused rather than worked around. The loop
    below removes a piece of an over-long paragraph each time round, and the
    size of that piece is the budget: at zero it removes nothing and asks the
    same question again, forever, holding the pipeline with nothing to show
    for it. config.toml refuses the value that gets here, and this says so a
    second time for any other caller.
    """
    if max_chars < 1:
        raise ValueError(f"chunk budget must be at least one character, got {max_chars}")
    paragraphs = [p.strip() for p in normalize_newlines(text).split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for p in paragraphs:
        while len(p) > max_chars:
            cut = _split_point(p, max_chars)
            chunks.append(p[:cut].strip())
            p = p[cut:].lstrip()
        if current and len(current) + len(p) + 2 > max_chars:
            chunks.append(current)
            current = p
        else:
            current = f"{current}\n\n{p}" if current else p
    if current:
        chunks.append(current)
    return chunks


def _prefer_copies_to_symlinks() -> None:
    """Ask huggingface_hub for the fallback it is going to use anyway.

    It links blobs into snapshots by default, which Windows refuses without
    Developer Mode or an administrator. It recovers by copying -- but only
    after fastembed has logged "Could not download model from either source"
    in red and slept through its retries, so the first command a new owner
    runs printed two errors and then worked. Nothing was wrong; say so up
    front by choosing the copy before anything fails.
    """
    if os.name != "nt":
        return
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    # Redundant once the symlinks are not attempted, and it names an
    # environment variable at a person who has not asked for one.
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    # The constants are read once at import. If something has already pulled
    # the library in, the environment above arrived too late for it.
    constants = sys.modules.get("huggingface_hub.constants")
    if constants is not None:
        constants.HF_HUB_DISABLE_SYMLINKS = True
        constants.HF_HUB_DISABLE_SYMLINKS_WARNING = True


def _quiet_hub_notices() -> None:
    """Keep the Hub's own notices out of a first run.

    The Hub answers an anonymous download with an `X-HF-Warning` header that
    huggingface_hub logs as "set a HF_TOKEN to enable higher rate limits".
    Varyz downloads two public models once; a new owner has no token to set
    and nothing to do about it. Errors still get through.
    """
    import logging

    logging.getLogger("huggingface_hub.utils._http").setLevel(logging.ERROR)


class Embedder:
    """Lazy wrapper around fastembed; first use downloads the model (~220MB)
    into data/models."""

    def __init__(self, cfg: Config):
        self.model_name = cfg.embedding.model
        # Internal override for isolated/offline evaluation archives. It points
        # at model weights only, never at another archive or its database.
        self.cache_dir = os.environ.get(
            "VARYZ_EMBEDDING_CACHE", str(cfg.data_dir / "models")
        )
        self._model = None

    def _ensure(self):
        if self._model is None:
            import warnings

            _prefer_copies_to_symlinks()
            _quiet_hub_notices()
            from fastembed import TextEmbedding

            self._announce_first_download(TextEmbedding)
            with warnings.catch_warnings():
                # fastembed warns about a pooling change vs older releases; we
                # never embedded with the old behaviour, so it's our baseline.
                warnings.filterwarnings("ignore", message=".*mean pooling.*")
                self._model = TextEmbedding(self.model_name, cache_dir=self.cache_dir)
        return self._model

    def _announce_first_download(self, text_embedding: type) -> None:
        """Name the download before fastembed starts it.

        The first command that embeds anything stops for a quarter of a
        gigabyte, and all fastembed says about it is a bare "Fetching 5 files"
        bar: no name, no size, and no word that it happens once. Said plainly
        it is a wait; unsaid it looks like `varyz add` has hung. Written to
        stderr, where that bar already goes, because stdout on the MCP server
        carries the protocol.
        """
        if any(Path(self.cache_dir).glob("models--*")):
            return
        try:
            entry = next(
                item
                for item in text_embedding.list_supported_models()
                if item["model"] == self.model_name
            )
            size = f" (about {round(float(entry["size_in_GB"]) * 1000):,} MB)"
        except Exception:
            # A registry that has moved on is a reason to drop the number, not
            # to go back to saying nothing.
            size = ""
        print(
            f"First use: fetching the local search model{size}. This happens once.",
            file=sys.stderr,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._ensure().embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._ensure().query_embed(text))).tolist()


def embed_pending(
    conn: sqlite3.Connection,
    cfg: Config,
    embedder: Embedder,
    batch_size: int = 64,
    on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """Chunk entries without chunks, embed chunks not yet on the current model.
    Returns number of chunks embedded. ``on_progress`` hears (embedded, total)
    after each batch, once the model is loaded."""
    max_chars = cfg.embedding.chunk_tokens * CHARS_PER_TOKEN
    recipe = chunk_recipe(max_chars)

    for entry in dao.entries_needing_chunks(conn, recipe):
        text = f"{entry.title}\n\n{entry.body}" if entry.title else entry.body
        with conn:
            dao.replace_chunks(conn, entry.id, chunk_text(text, max_chars), recipe)

    if isinstance(embedder, Embedder):
        # The index is not usable for `recall` without its answer-checking
        # model, and this is the one step every way of building the index
        # shares. A test's stand-in embedder is not the real index.
        from varyz.retrieval.rerank import fetch_reranker

        fetch_reranker(cfg)

    pending = dao.chunks_needing_embedding(conn, embedder.model_name)
    if not pending:
        # Checked before the width alignment below, which needs a real vector
        # and so would download the model just to confirm there is no work.
        return 0

    # The vector index is fixed at the width of whichever model built it, and
    # vec0 only objects when a row is inserted — so swapping `embedding.model`
    # for one of a different size used to fail deep inside the loop below,
    # after the download and the chunking, with a bare SQLite error. Align the
    # index with the model that is actually producing the vectors instead.
    width = len(embedder.embed_query("dimension probe"))
    if dao.vector_width(conn) != width:
        dao.resize_vector_index(conn, width)
        pending = dao.chunks_needing_embedding(conn, embedder.model_name)

    embedded = 0
    if on_progress is not None:
        on_progress(0, len(pending))
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        vectors = embedder.embed_documents([row["text"] for row in batch])
        with conn:
            dao.store_embeddings(conn, [row["id"] for row in batch], vectors, embedder.model_name)
        embedded += len(batch)
        if on_progress is not None:
            on_progress(embedded, len(pending))

    with conn:
        conn.execute(
            """UPDATE enrichment_jobs SET status='done'
               WHERE task='embed' AND status='pending'
                 AND NOT EXISTS (SELECT 1 FROM chunks c
                                 WHERE c.entry_id = enrichment_jobs.entry_id
                                   AND (c.embed_model IS NULL OR c.embed_model != ?))""",
            (embedder.model_name,),
        )
    return embedded
