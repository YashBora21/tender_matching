"""Retrieval (FR-17 to FR-20).

A requirement is answered by the documents that describe the same thing. Two
searches run over the corpus and their results are merged:

- **keyword**: PostgreSQL full-text search over the chunk text, with the
  exact tokens a requirement carries - a model number, a standard code, a
  material grade - boosted, because those are the words that identify a
  product (FR-19).
- **semantic**: the embedding of the requirement against the embedding of
  every chunk.

The two lists are merged with reciprocal rank fusion, which needs no score
calibration between the two searches: a chunk that both searches rank highly
comes first, and neither search's scale can drown the other out (FR-18).

The retrieval is generic. It does not know what a valve is, and it does not
name a manufacturer: the aliases and the categories come from the database
and the filters are passed in.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sqlalchemy import func, literal, literal_column, or_, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.repositories.schema import chunk, document, manufacturer, product
from app.services.llm import embed

logger = get_logger(__name__)

# The rank at which a list's contribution falls off. The usual value: the
# first result counts a lot, the tenth a little.
RRF_K = 60

# A token that identifies a product: a model number, a standard code, a
# material grade. These are the words a requirement and a datasheet both use
# to name the same thing, so they are worth more than a common word.
_IDENTIFIER = re.compile(
    r"\b(?:ss\s?\d{3,4}[l]?|cf\s?\d+[a-z]?|aisi\s?\d{3,4}|"
    r"is\s?\d+|iso\s?\d+|astm\s?[a-z]?\s?\d+|api\s?\d+|din\s?\d+|bs\s?\d+|"
    r"nace\s?\w+|dn\s?\d+|nps\s?\d+|ip\s?\d{2}|pn\s?\d+|dn\d+|"
    r"\d+\s?(?:mm|cm|mtr|bar|kg|rpm|kw|v|a|%))\b",
    re.IGNORECASE,
)


@dataclass
class Candidate:
    """One chunk the retrieval found, with where it came from."""

    chunk_id: str
    document_id: str
    text: str
    page: Optional[int] = None
    section: Optional[str] = None
    is_table: bool = False
    ocr: bool = False
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    family: Optional[str] = None
    # What the document itself declares it covers, and the file name it came
    # from. Entity validity is decided from this metadata, not from the
    # passage (D-94), and the title is part of what a chunk is embedded with
    # (D-97).
    model_or_family: Optional[str] = None
    document_title: Optional[str] = None
    # True when the document is about a family of products rather than one
    # model. Evidence from it is family-level: uncertain applicability
    # (PRD 4).
    is_family_level: bool = False
    # The type the *document* declares for itself, and whether it could be
    # established at all (D-100). A document whose type is unknown cannot be
    # shown to be about the wanted type, so its evidence is family-level.
    document_type: Optional[str] = None
    type_unconfirmed: bool = False
    # The ranks the two searches gave it, and the merged score.
    keyword_rank: Optional[int] = None
    semantic_rank: Optional[int] = None
    score: float = 0.0
    # The exact tokens the requirement shares with this chunk (FR-19).
    shared_identifiers: list[str] = field(default_factory=list)

    @property
    def document_path(self) -> Optional[str]:
        return None


def identifiers(text: str) -> list[str]:
    """The tokens in a text that identify a product.

    A token is returned in the forms it can be written in: as it stands
    ("SS316", "150 mm"), with its parts joined ("ss316", "150mm"), and - when
    it carries a number - the number on its own ("150"). The last one matters
    because a datasheet table writes the value in the cell and the unit in
    the column header, so "267" is on its own and "267 mm" never appears.
    """
    found: list[str] = []
    for match in _IDENTIFIER.finditer(text):
        token = match.group(0).lower()
        found.append(token)
        joined = re.sub(r"[^a-z0-9]", "", token)
        if joined and joined != token:
            found.append(joined)
        number = re.search(r"\d+(?:\.\d+)?", token)
        if number and number.group(0) not in found:
            found.append(number.group(0))
    return found


def _or_query(query: str) -> str:
    """The words of a query, ORed together.

    The words are quoted so that a word with a character the query language
    reads - a hyphen, a colon, a bracket - cannot turn the query into
    something else. The common words are left out: a chunk that says
    "the" matches every requirement, and a chunk that matches *every* word
    of the requirement is the one that answers it.
    """
    words = [word for word in re.findall(r"[A-Za-z0-9]+", query) if word.lower() not in _STOPWORDS]
    if not words:
        # Every word was a common one: take them all rather than nothing.
        words = re.findall(r"[A-Za-z0-9]+", query)
    if not words:
        return ""
    return " | ".join(f"'{word}'" for word in words)


# The words that match every document and answer none of them.
_STOPWORDS = frozenset(
    """
    a an the of for with and or shall must should be been being is are was were
    to in on at by as it its this that these those from not no nor so such
    than then there their they them we our you your i he she his her
    have has had do does did will would can could may might
    if but when where which who whom what how why
    """.split()
)


def _tsquery(or_query: str):
    """A query as a tsquery, so the words are read as words and not as
    operators."""
    from sqlalchemy.dialects.postgresql import TSQUERY

    return literal(or_query, type_=TSQUERY)


def _category_filter(category: Optional[str]):
    """The condition that keeps a requirement inside its own category (FR-17).

    A document that declares a *different* product category is not evidence
    for this item, and is excluded. A document that declares nothing, or
    declares something that is not a product category at all - an older
    manifest wrote the manufacturer's slug there - is kept: a missing
    category is not a category that says "not this one", and filtering on
    equality alone would drop the whole corpus and report the silence as
    "no evidence found" (PRD 4).
    """
    if not category:
        return None
    from app.services.match.category import category_names

    others = category_names() - {category}
    if not others:
        return None
    return or_(document.c.category.is_(None), document.c.category.notin_(others))


def _role_filter():
    """The condition that keeps a document that is not product evidence out.

    A privacy policy is not a datasheet: it answers no requirement, and a page
    of it that happens to contain the requirement's words would be published as
    evidence. Which roles mean that comes from `data/doc_roles.json`, so the
    decision is an edit to the vocabulary and not a list in retrieval code.

    A document whose role was never resolved is kept. Silence is not a policy
    (PRD 4), and filtering on inequality alone would drop every document the
    corpus has not read yet.
    """
    from app.services.corpus.classify import excluded_roles

    excluded = excluded_roles()
    if not excluded:
        return None
    return or_(document.c.doc_role.is_(None), document.c.doc_role.notin_(excluded))


def _document_title(path: Optional[str]) -> Optional[str]:
    """The title a document is known by: its file name without the extension."""
    if not path:
        return None
    return Path(str(path)).stem or None


def _type_filter(
    candidates: list[Candidate],
    category: Optional[str],
    product_type: Optional[str],
) -> list[Candidate]:
    """Drop the chunks that belong to a different product type (D-97, D-100).

    A gate-valve requirement is not answered by a ball-valve datasheet, even
    though both are `valves`. A chunk that names no type at all is kept,
    because silence is not a different type and dropping it would turn a
    corpus gap into a missing capability (PRD 4).

    Silence is resolved one level up. A page that never says "ball valve" is
    still in a ball-valve catalogue, and the cover says so; the document's own
    declared model or family decides the type (D-100), and when it names a
    different type the whole document goes, even if the page happens to
    mention the wanted one (D-107). Only when neither the chunk nor the
    document names a type is the answer left open: the chunk is kept, and the
    evidence is marked family-level so a reviewer sees that the type was never
    confirmed.
    """
    if not category or not product_type:
        return candidates
    from app.services.match.category import document_type, mentioned_types

    kept: list[Candidate] = []
    for candidate in candidates:
        named = mentioned_types(category, candidate.text, candidate.section)
        declared = document_type(
            category,
            candidate.model_or_family,
            candidate.family,
            candidate.document_title,
        )
        candidate.document_type = declared.product_type
        if declared.product_type:
            # The document says what it is about, and that decides for the
            # whole document. A page in a control-valve handbook that happens
            # to mention ball valves is not a ball-valve datasheet (D-107).
            if declared.product_type != product_type:
                continue
            kept.append(candidate)
            continue
        if named and product_type not in named:
            continue
        if not named:
            # Neither the page nor the document says what it is. That is not
            # a different type, and it is not a confirmed one either.
            candidate.type_unconfirmed = True
        kept.append(candidate)
    return kept


def _category_scope_filter(
    candidates: list[Candidate],
    category: Optional[str],
) -> list[Candidate]:
    """Drop a document that declares a different product category (D-107).

    The stored `document.category` column is the first check, in SQL, and it is
    empty for a corpus whose manifest never filled it. The document's own name
    and declared model still say what it is - "Goulds wastewater pumps",
    "lv-motors-hazardous-area-application" - and a pump catalogue is not
    evidence for a valve item.

    The rule is the one the type filter already uses: a document that names the
    wanted category is kept, a document that names none is kept (silence is not
    a different category), and only a document that positively names another
    category and not this one is dropped.
    """
    if not category or not candidates:
        return candidates
    from app.services.match.category import mentioned_categories

    kept: list[Candidate] = []
    for candidate in candidates:
        named = mentioned_categories(
            candidate.model_or_family,
            candidate.family,
            candidate.document_title,
        )
        if named and category not in named:
            continue
        kept.append(candidate)
    return kept


def _mentions(text: str, term: str) -> bool:
    """True when a term appears in the text as a whole phrase."""
    return bool(re.search(rf"\b{re.escape(term.casefold())}\b", text))


def _expand_text(text: str, groups: dict[str, list[str]]) -> str:
    """Add the other names of an attribute the text already names.

    The groups come from the alias table, keyed by canonical term. A group is
    used only when the text mentions one of its names, and a name already in
    the text is not added again: repeating a word would weight it twice in the
    keyword rank (D-96, D-102).
    """
    lowered = text.casefold()
    known = {word.casefold() for word in text.split()}
    additions: list[str] = []
    for aliases in groups.values():
        if not any(_mentions(lowered, alias) for alias in aliases):
            continue
        for alias in aliases:
            key = alias.casefold()
            if key in known or _mentions(lowered, alias):
                continue
            known.add(key)
            additions.append(alias)
    return " ".join([text, *additions]) if additions else text


def expand_query(session: Session, text: str) -> str:
    """Widen a query with the alias table's other names for an attribute.

    A requirement says "screwed end" and the datasheet says "body style": the
    two name the same attribute, and only the alias table knows that. The
    vocabulary says which attributes are worth expanding, so a common word
    such as "pressure" does not pull every synonym into every query (D-102).
    """
    if session is None or not text.strip():
        return text
    from app.comparison.attributes import load_vocabulary
    from app.repositories.aliases import AliasRepo

    wanted = {
        spec.canonical_term
        for spec in load_vocabulary().attributes
        if spec.query_expansion
    }
    if not wanted:
        return text
    groups = AliasRepo(session).approved_aliases(
        threshold=get_settings().alias_confidence_threshold
    )
    return _expand_text(
        text, {term: aliases for term, aliases in groups.items() if term in wanted}
    )


def _candidate_from_row(row) -> Candidate:
    """One chunk row, in the column order the searches select."""
    return Candidate(
        chunk_id=str(row[0]),
        document_id=str(row[1]),
        text=row[2],
        page=row[3],
        section=row[4],
        is_table=bool(row[5]),
        ocr=bool(row[6]),
        manufacturer=row[7],
        model=row[8],
        family=row[9],
        is_family_level=bool(row[10]),
        model_or_family=row[11],
        document_title=_document_title(row[12]),
    )


def _eligible_documents(
    page_counts: dict[str, Optional[int]], page_limit: int
) -> set[str]:
    """The documents short enough to read whole.

    The count is the highest indexed page: a document whose last page carries
    no chunk is treated as ending at its last chunk, which is the part the
    corpus can actually offer (D-102).
    """
    return {
        document_id
        for document_id, pages in page_counts.items()
        if pages is not None and pages <= page_limit
    }


def _merge_siblings(
    candidates: list[Candidate],
    siblings: dict[str, list[Candidate]],
    *,
    limit: int,
) -> list[Candidate]:
    """Insert a short document's pages after its first retrieved chunk.

    The fused order of the original candidates is kept: siblings are added
    beside the hit they belong to, in page order, and the result is trimmed to
    the same budget. A sibling the search already found is not repeated.
    """
    merged: list[Candidate] = []
    seen: set[str] = set()
    expanded: set[str] = set()
    # The chunks of a document that was expanded because it is short. They are
    # one unit: the cap trims the candidates around them, never the pages the
    # document was expanded into.
    whole: set[str] = set()
    for candidate in candidates:
        if candidate.chunk_id not in seen:
            merged.append(candidate)
            seen.add(candidate.chunk_id)
        document_id = candidate.document_id
        if document_id in expanded or document_id not in siblings:
            continue
        expanded.add(document_id)
        whole.add(candidate.chunk_id)
        for sibling in siblings[document_id]:
            if sibling.chunk_id in seen:
                continue
            merged.append(sibling)
            seen.add(sibling.chunk_id)
            whole.add(sibling.chunk_id)
    if len(merged) <= limit:
        return merged
    # Dropping a sibling to make the budget would read a short document only
    # halfway and report the page that was cut as silent (D-107). The lowest
    # ranked candidates outside the document are the ones the cap removes.
    trimmed = list(merged)
    index = len(trimmed) - 1
    while len(trimmed) > limit and index >= 0:
        if trimmed[index].chunk_id not in whole:
            del trimmed[index]
        index -= 1
    return trimmed


def expand_siblings(
    session: Session,
    candidates: list[Candidate],
    *,
    category: Optional[str],
    product_type: Optional[str],
    limit: int,
) -> list[Candidate]:
    """Read a short document whole when one of its pages was retrieved.

    A datasheet's cover page and its specification page rarely share words, so
    a page-level hit on a three-page bulletin can hide the page that carries
    the values. The siblings are filtered by the same category and type rules
    as any other candidate; the global ranking is not re-scored (D-102).
    """
    if session is None or not candidates:
        return candidates[:limit]
    page_limit = get_settings().retrieval_sibling_page_limit
    if page_limit <= 0:
        return candidates[:limit]

    document_ids = {
        uuid.UUID(candidate.document_id)
        for candidate in candidates
        if candidate.document_id
    }
    if not document_ids:
        return candidates[:limit]
    counts = {
        str(row[0]): row[1]
        for row in session.execute(
            select(chunk.c.document_id, func.max(chunk.c.page))
            .where(chunk.c.document_id.in_(document_ids))
            .group_by(chunk.c.document_id)
        ).all()
    }
    eligible = _eligible_documents(counts, page_limit)
    if not eligible:
        return candidates[:limit]

    rows = session.execute(
        select(
            chunk.c.id,
            chunk.c.document_id,
            chunk.c.text,
            chunk.c.page,
            chunk.c.section,
            chunk.c.is_table,
            chunk.c.ocr,
            manufacturer.c.name,
            product.c.model,
            product.c.family,
            product.c.is_family_level,
            document.c.model_or_family,
            document.c.path,
        )
        .select_from(chunk)
        .join(document, document.c.id == chunk.c.document_id)
        .join(manufacturer, manufacturer.c.id == document.c.manufacturer_id)
        .outerjoin(product, product.c.id == document.c.product_id)
        .where(chunk.c.document_id.in_(eligible))
        .order_by(chunk.c.document_id, chunk.c.page, chunk.c.id)
    ).all()

    siblings: dict[str, list[Candidate]] = {}
    for row in rows:
        candidate = _candidate_from_row(row)
        siblings.setdefault(candidate.document_id, []).append(candidate)
    for document_id, items in siblings.items():
        siblings[document_id] = _type_filter(items, category, product_type)
    return _merge_siblings(candidates, siblings, limit=limit)


def keyword_search(
    session: Session,
    query_text: str,
    *,
    limit: int = 50,
    manufacturer_name: Optional[str] = None,
    category: Optional[str] = None,
    model: Optional[str] = None,
) -> list[Candidate]:
    """The chunks that match a requirement's words.

    The words are ORed, not ANDed. A requirement names several things at
    once - "Body shall be SS316 with pressure rating 16 bar" - and no
    single datasheet sentence has all of them, so an AND finds nothing at
    all. The ranking puts the chunks that match *more* of the words first,
    and the exact tokens are counted on top: a requirement that names
    "SS316" wants the chunks that say "SS316", not the many that say
    "stainless" (FR-19).
    """
    query = query_text.strip()
    if not query:
        return []

    statement = (
        select(
            chunk.c.id,
            chunk.c.document_id,
            chunk.c.text,
            chunk.c.page,
            chunk.c.section,
            chunk.c.is_table,
            chunk.c.ocr,
            manufacturer.c.name,
            product.c.model,
            product.c.family,
            product.c.is_family_level,
            document.c.model_or_family,
            document.c.path,
            func.ts_rank_cd(chunk.c.search_vector, _tsquery(_or_query(query))).label("rank"),
        )
        .select_from(chunk)
        .join(document, document.c.id == chunk.c.document_id)
        .join(manufacturer, manufacturer.c.id == document.c.manufacturer_id)
        # The document's own product, and not every product of its
        # manufacturer: the same passage read once per product is the same
        # passage read many times, under a name it is not about.
        .outerjoin(product, product.c.id == document.c.product_id)
        .where(chunk.c.search_vector.op("@@")(_tsquery(_or_query(query))))
    )
    if manufacturer_name:
        statement = statement.where(manufacturer.c.name == manufacturer_name)
    condition = _category_filter(category)
    if condition is not None:
        statement = statement.where(condition)
    condition = _role_filter()
    if condition is not None:
        statement = statement.where(condition)
    if model:
        statement = statement.where(product.c.model == model)
    # More rows are fetched than are returned: the exact-token boost below
    # re-ranks them, and a chunk that shares a token with the requirement
    # would be missed if the database cut the list before the boost.
    statement = statement.order_by(literal_column("rank DESC")).limit(
        max(limit * 10, 100)
    )

    wanted = identifiers(query_text)
    found: list[Candidate] = []
    for row in session.execute(statement):
        candidate = Candidate(
            chunk_id=str(row[0]),
            document_id=str(row[1]),
            text=row[2],
            page=row[3],
            section=row[4],
            is_table=bool(row[5]),
            ocr=bool(row[6]),
            manufacturer=row[7],
            model=row[8],
            family=row[9],
            is_family_level=bool(row[10]),
            model_or_family=row[11],
            document_title=_document_title(row[12]),
        )
        if wanted:
            lowered = candidate.text.lower()
            # A token matches the text as it stands, and with its parts
            # joined: the corpus writes "150 MM" where the requirement
            # writes "150 mm".
            squashed = re.sub(r"[^a-z0-9]", "", lowered)
            candidate.shared_identifiers = [
                token
                for token in wanted
                if token in lowered or re.sub(r"[^a-z0-9]", "", token) in squashed
            ]
        found.append(candidate)

    # The chunks that share an exact token with the requirement come first
    # (FR-19). A requirement that names "SS316" wants the datasheet that
    # says "SS316", not the many that say "stainless": the full-text rank
    # cannot tell them apart, because "stainless" is the common word and
    # "ss316" is the one that identifies the product. The sort is stable, so
    # the full-text rank still decides between chunks that share the same
    # number of tokens.
    if wanted:
        found.sort(key=lambda c: len(c.shared_identifiers), reverse=True)
    return found[:limit]


def semantic_search(
    session: Session,
    query_text: str,
    *,
    limit: int = 50,
    manufacturer_name: Optional[str] = None,
    category: Optional[str] = None,
    model: Optional[str] = None,
) -> list[Candidate]:
    """The chunks whose meaning is closest to a requirement.

    The corpus is embedded once, and the requirement is embedded per search.
    A chunk with no embedding is not a candidate: it has no place in this
    search, and the keyword search is still there for it.
    """
    if not query_text.strip():
        return []
    try:
        info = getattr(session, "info", {})
        cache = info.get("requirement_embeddings")
        settings = get_settings()
        key = (settings.embedding_backend, settings.embedding_model,
               settings.embedding_base_url, settings.embedding_dimension, query_text)
        vectors = cache.get(key) if cache is not None else None
        if vectors is None:
            vectors = embed([query_text])
            if cache is not None and vectors:
                cache[key] = vectors
    except Exception as exc:  # noqa: BLE001 - a search that cannot embed finds nothing
        logger.warning("could not embed the query: %s", exc)
        return []
    if not vectors:
        return []

    vector = vectors[0]
    distance = chunk.c.embedding.cosine_distance(vector)
    statement = (
        select(
            chunk.c.id,
            chunk.c.document_id,
            chunk.c.text,
            chunk.c.page,
            chunk.c.section,
            chunk.c.is_table,
            chunk.c.ocr,
            manufacturer.c.name,
            product.c.model,
            product.c.family,
            product.c.is_family_level,
            document.c.model_or_family,
            document.c.path,
            distance.label("distance"),
        )
        .select_from(chunk)
        .join(document, document.c.id == chunk.c.document_id)
        .join(manufacturer, manufacturer.c.id == document.c.manufacturer_id)
        .outerjoin(product, product.c.id == document.c.product_id)
        .where(chunk.c.embedding.isnot(None))
    )
    if manufacturer_name:
        statement = statement.where(manufacturer.c.name == manufacturer_name)
    condition = _category_filter(category)
    if condition is not None:
        statement = statement.where(condition)
    condition = _role_filter()
    if condition is not None:
        statement = statement.where(condition)
    if model:
        statement = statement.where(product.c.model == model)
    statement = statement.order_by(distance).limit(limit)

    return [
        Candidate(
            chunk_id=str(row[0]),
            document_id=str(row[1]),
            text=row[2],
            page=row[3],
            section=row[4],
            is_table=bool(row[5]),
            ocr=bool(row[6]),
            manufacturer=row[7],
            model=row[8],
            family=row[9],
            is_family_level=bool(row[10]),
            model_or_family=row[11],
            document_title=_document_title(row[12]),
        )
        for row in session.execute(statement)
    ]


def fuse(
    keyword: list[Candidate],
    semantic: list[Candidate],
    *,
    limit: int = 20,
) -> list[Candidate]:
    """Merge the two rankings (FR-18).

    Reciprocal rank fusion: a chunk's score is the sum of what each search
    gives it for its position. The scales of the two searches never have to
    agree, because only the order is used.
    """
    merged: dict[str, Candidate] = {}
    scores: dict[str, float] = {}

    for rank, candidate in enumerate(keyword, start=1):
        merged[candidate.chunk_id] = candidate
        candidate.keyword_rank = rank
        scores[candidate.chunk_id] = scores.get(candidate.chunk_id, 0.0) + 1.0 / (
            RRF_K + rank
        )

    for rank, candidate in enumerate(semantic, start=1):
        existing = merged.get(candidate.chunk_id)
        if existing is None:
            merged[candidate.chunk_id] = candidate
            existing = candidate
        existing.semantic_rank = rank
        scores[candidate.chunk_id] = scores.get(candidate.chunk_id, 0.0) + 1.0 / (
            RRF_K + rank
        )

    for chunk_id, candidate in merged.items():
        candidate.score = scores.get(chunk_id, 0.0)

    return sorted(merged.values(), key=lambda c: c.score, reverse=True)[:limit]


def search(
    session: Session,
    text: str,
    *,
    limit: int = 20,
    manufacturer_name: Optional[str] = None,
    category: Optional[str] = None,
    product_type: Optional[str] = None,
    model: Optional[str] = None,
) -> list[Candidate]:
    """Find the chunks that answer a requirement.

    Both searches run and their results are merged. A deployment that has no
    embedding model still gets the keyword half. The query is widened with the
    alias table's other names for an attribute it names, a document that names
    another product category is dropped (D-107), and a short document is read
    whole when one of its pages is retrieved (D-102).
    """
    text = expand_query(session, text)
    keyword = _category_scope_filter(
        _type_filter(
            keyword_search(
                session,
                text,
                manufacturer_name=manufacturer_name,
                category=category,
                model=model,
            ),
            category,
            product_type,
        ),
        category,
    )
    semantic = _category_scope_filter(
        _type_filter(
            semantic_search(
                session,
                text,
                manufacturer_name=manufacturer_name,
                category=category,
                model=model,
            ),
            category,
            product_type,
        ),
        category,
    )
    if not semantic:
        # No embedding model, or no embedded corpus: the keyword search is
        # the whole answer.
        for rank, candidate in enumerate(keyword, start=1):
            candidate.keyword_rank = rank
        return expand_siblings(
            session,
            keyword[:limit],
            category=category,
            product_type=product_type,
            limit=limit,
        )
    fused = fuse(keyword, semantic, limit=limit)
    return expand_siblings(
        session,
        fused,
        category=category,
        product_type=product_type,
        limit=limit,
    )
