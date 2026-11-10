"""Embeddings: the local model is the default, the HTTP shape still works, and
a batch is written in one statement."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.repositories.postgres import PostgresDocumentRepo
from app.repositories.schema import chunk
from app.services import embeddings, llm


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_local_backend_encodes_with_normalised_vectors(monkeypatch):
    """The default backend loads the weights in this process: no account, no
    credits. bge models are trained for cosine, so the vector is normalised."""
    monkeypatch.setenv("EMBEDDING_BACKEND", "sentence-transformers")
    monkeypatch.setenv("EMBEDDING_MODEL", "synthetic-embedding-model")
    get_settings.cache_clear()
    seen: dict = {}

    class FakeModel:
        def encode(self, texts, **kwargs):
            seen["texts"] = texts
            seen.update(kwargs)
            return [[0.1, 0.2], [0.3, 0.4]]

    monkeypatch.setattr(llm, "_sentence_transformer", lambda name: FakeModel())

    vectors = llm.embed(["a", "b"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert seen["texts"] == ["a", "b"]
    assert seen["normalize_embeddings"] is True


def test_no_texts_is_no_call(monkeypatch):
    monkeypatch.setattr(
        llm, "_sentence_transformer", lambda name: pytest.fail("must not load a model")
    )

    assert llm.embed([]) == []


def test_the_http_backend_posts_the_openai_embeddings_shape(monkeypatch):
    """The alternative backend is any OpenAI-compatible endpoint, such as a
    local Ollama. It is config, not a second implementation."""
    monkeypatch.setenv("EMBEDDING_BACKEND", "http")
    monkeypatch.setenv("EMBEDDING_MODEL", "synthetic-http-embedding")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://synthetic-embedding.example.test")
    get_settings.cache_clear()
    sent: dict = {}

    def fake_urlopen(request, timeout=None):
        sent["url"] = request.full_url
        sent["body"] = json.loads(request.data)
        return _Response({"data": [{"index": 0, "embedding": [0.5, 0.6]}]})

    monkeypatch.setattr("app.services.llm.urllib.request.urlopen", fake_urlopen)

    assert llm.embed(["a"]) == [[0.5, 0.6]]
    assert sent["url"] == "https://synthetic-embedding.example.test/v1/embeddings"
    assert sent["body"] == {"model": "synthetic-http-embedding", "input": ["a"]}


def test_an_unknown_backend_is_refused(monkeypatch):
    """A typo in the environment must not silently produce no vectors."""
    monkeypatch.setenv("EMBEDDING_BACKEND", "sentence-transformerss")
    get_settings.cache_clear()

    with pytest.raises(llm.LLMError) as caught:
        llm.embed(["a"])

    assert "unknown embedding backend" in str(caught.value)


def test_a_vector_literal_keeps_full_precision():
    """The stored vector must be the one the model produced.

    `repr` of a float round-trips, so parsing the literal gives the same
    numbers back; a formatted copy would not.
    """
    vector = [0.1, -1.0 / 3.0, 1e-7, 0.0]

    literal = embeddings._vector_literal(vector)

    assert literal.startswith("[") and literal.endswith("]")
    assert [float(part) for part in literal[1:-1].split(",")] == vector


def test_a_chunk_is_embedded_with_the_document_and_the_table_caption():
    """A table row is a number and its column header. Embedded alone it is
    close to every other row of numbers; the header names what it is about
    (D-97)."""
    text = embeddings.embedding_text(
        "150",
        document_title="CIRCOR-D-Series",
        manufacturer="CIRCOR",
        product="D Series",
        section="Pressure class",
    )

    assert "CIRCOR" in text
    assert "D Series" in text
    assert "Pressure class" in text
    assert "150" in text
    # The header comes first: the chunk is the body.
    assert text.index("CIRCOR") < text.index("150")


def test_a_chunk_with_no_metadata_is_embedded_as_its_text():
    assert embeddings.embedding_text("Body shall be SS316") == "Body shall be SS316"


def test_the_product_label_prefers_the_family():
    """A datasheet page is about the series, not one catalogue number."""
    assert embeddings._product_label("D Series", "D-100", "D") == "D Series"
    assert embeddings._product_label(None, "D-100", "D") == "D"
    assert embeddings._product_label(None, "D-100", None) == "D-100"


@pytest.fixture
def rollback_session(test_database_url: str):
    """A session whose writes are rolled back at the end."""
    engine = create_engine(test_database_url, future=True)
    with Session(engine, future=True) as db:
        transaction = db.begin()
        yield db
        transaction.rollback()
    engine.dispose()


@pytest.mark.integration
def test_a_batch_of_embeddings_is_written_in_one_statement(rollback_session):
    """One round trip per batch, not one per chunk.

    The local database hides the difference: a round trip there costs almost
    nothing. Against Neon one round trip measured 300 ms, so a batch of 32 spent
    9 s waiting on the network around 30 ms of encoding (D-83).
    """
    repo = PostgresDocumentRepo(rollback_session)
    document_id = repo.upsert_document(
        {
            "manufacturer": "Acme Valves",
            "url": "https://acme.example/embeddings.pdf",
            "sha256": "c" * 64,
            "doc_type": "datasheet",
            "path": "raw/embeddings.pdf",
        }
    )
    repo.insert_chunks(
        document_id, [{"page": 1, "text": f"chunk number {index}"} for index in range(32)]
    )
    rollback_session.flush()

    ids = [
        row[0]
        for row in rollback_session.execute(
            select(chunk.c.id).where(chunk.c.document_id == UUID(document_id))
        ).all()
    ]
    assert len(ids) == 32

    statements: list[str] = []

    def record(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("update"):
            statements.append(statement)

    engine = rollback_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    vectors = [[index / 1000.0] * 384 for index in range(32)]
    try:
        embeddings._write_embeddings(rollback_session, list(zip(ids, vectors)))
        rollback_session.flush()
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(statements) == 1, "a batch must cost one round trip, not one per chunk"

    stored = {
        row[0]: [float(value) for value in row[1]]
        for row in rollback_session.execute(
            select(chunk.c.id, chunk.c.embedding).where(chunk.c.id.in_(ids))
        ).all()
    }
    assert len(stored) == 32
    assert all(stored[chunk_id] == vector for chunk_id, vector in zip(ids, vectors))
