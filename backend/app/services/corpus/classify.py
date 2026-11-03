"""What each corpus document is, read from its own words.

Retrieval compares the product type a requirement needs with the type the
candidate's product declares, and falls back to family-level evidence - a capped
score and a review flag - when the document never said (PRD 4, D-100). The type
column has never been written, so *every* candidate is answered family-level
however exact its datasheet. This module fills the declaration from data: the
type vocabulary is the one `app.services.match.category` already reads from
`data/categories.json`, and the roles a document can play are in
`data/doc_roles.json`.

No model is asked. A document the rules cannot place stays unresolved and is
written to the reviewer's CSV rather than guessed at, and a value that is already
there is never overwritten, because it may be a decision a person made.

Run it::

    python -m app.services.corpus.classify              # read only, writes a CSV
    python -m app.services.corpus.classify --apply      # fill the empty columns
    python -m app.services.corpus.classify --load eval/product_types_todo.csv
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.repositories.schema import document, manufacturer, product

__all__ = [
    "ClassifyReport",
    "classify_corpus",
    "doc_role",
    "document_type_of",
    "excluded_roles",
    "export_unresolved",
    "load_types",
    "main",
]

DEFAULT_DOC_ROLES_PATH = Path(__file__).resolve().parents[3] / "data" / "doc_roles.json"

# The reviewer's worklist lives in the repo's own eval/ beside the gold labels,
# so it must not depend on which directory the pass was started from.
DEFAULT_TODO_PATH = Path(__file__).resolve().parents[4] / "eval" / "product_types_todo.csv"

# How much of the opening pages is searched for a role. A certificate says what
# it is on the first page; reading more would cost the whole corpus for nothing.
# shortcut: the first pages only, upgrade when a role is ever declared later.
ROLE_TEXT_CHARS = 2000


def _normalise(text: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


@dataclass(frozen=True)
class DocRole:
    """One role and the words that name it.

    `from_name_only` marks a role that takes a document out of the evidence -
    a privacy policy, a certificate. It is accepted only from the manifest or
    from what the document calls itself, never from a paragraph: a valve
    datasheet whose specification line reads "fire safe certified: api 607" was
    labelled a certificate by those words, which hides the product it is
    evidence for (D-153). A descriptive role - datasheet, catalogue - costs
    little when it is wrong, so the pages may name it.
    """

    name: str
    terms: tuple[str, ...]
    from_name_only: bool = False


@lru_cache(maxsize=4)
def _load(target: str) -> tuple[dict, tuple[DocRole, ...], tuple[str, ...]]:
    """The role vocabulary. A missing or broken file gives no roles."""
    try:
        data = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, (), ()
    declared = {
        _normalise(key): str(value).strip()
        for key, value in (data.get("declared") or {}).items()
        if _normalise(key)
    }
    roles = tuple(
        DocRole(
            name=str(entry.get("name") or "").strip(),
            terms=tuple(
                term for term in (_normalise(t) for t in entry.get("terms", ())) if term
            ),
            from_name_only=bool(entry.get("from_name_only")),
        )
        for entry in data.get("roles", ())
    )
    excluded = tuple(
        str(role).strip() for role in (data.get("retrieval_excluded") or ()) if str(role).strip()
    )
    return declared, tuple(entry for entry in roles if entry.name and entry.terms), excluded


def excluded_roles(path: Optional[str] = None) -> tuple[str, ...]:
    """The roles that mean *this document is not evidence about a product*.

    They come from the same data file as the roles themselves, so deciding that
    a privacy policy is not evidence is an edit to `data/doc_roles.json` and not
    a list in retrieval code. A certificate is deliberately absent: it is
    evidence for a certification requirement (PRD 4).
    """
    return _load(path or str(DEFAULT_DOC_ROLES_PATH))[2]


def doc_role(
    declared: Optional[str] = None,
    *parts: Optional[str],
    pages: Optional[str] = None,
    path: Optional[str] = None,
) -> Optional[str]:
    """The role a document plays, or None when nothing names it.

    What the collector's own manifest says wins - it is the one statement about
    the document made by whoever put it in the corpus. Only a document whose
    manifest stayed silent or wrote a word the map does not know is read from
    its own words. Two roles named equally is no answer, not a coin toss.

    `parts` is what the document calls itself - its file name and the model or
    family the corpus already holds. `pages` is the opening text, which may name
    a descriptive role but never one that takes the document out of the evidence
    (`from_name_only`, D-153).
    """
    mapping, roles, _ = _load(path or str(DEFAULT_DOC_ROLES_PATH))
    name = mapping.get(_normalise(declared))
    if name:
        return name
    cover = _normalise(" ".join(part or "" for part in parts))
    body = f"{cover} {_normalise(pages)}"
    hits = [
        (len(term), role.name)
        for role in roles
        for term in role.terms
        if term in (cover if role.from_name_only else body)
    ]
    if not hits:
        return None
    longest = max(length for length, _ in hits)
    tied = {found for length, found in hits if length == longest}
    return tied.pop() if len(tied) == 1 else None


def document_type_of(category: Optional[str], *parts: Optional[str]) -> Optional[str]:
    """The product type a document names inside the category the corpus gave it.

    The same inference an item title gets, from the same data file, so a
    document and a requirement are placed in one vocabulary (FR-16). A document
    that names two types equally stays untyped rather than filtered on a guess.

    The category is never inferred here. Reading it from the same opening pages
    the type is read from lets one stray word place a whole product: the corpus
    holds Delval and DelTorq *actuator* bulletins whose first pages mention the
    valve they mount, and that route typed them butterfly valves (D-149). A
    product whose category the manifest left blank is unresolved, not guessed.
    """
    from app.services.match.category import document_type

    kind = (category or "").strip()
    if not kind or kind == "unknown":
        return None
    return document_type(kind, *parts).product_type


@dataclass
class ClassifyReport:
    """What a pass read, filled and left for a reviewer."""

    documents: int = 0
    roles: int = 0
    types: int = 0
    unreadable: int = 0
    unresolved: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"documents read          {self.documents}\n"
            f"doc_role filled         {self.roles}\n"
            f"product_type filled     {self.types}\n"
            f"no page text at all     {self.unreadable}\n"
            f"left for a reviewer     {len(self.unresolved)}"
        )


def _first_pages(path: Path, pages: int) -> str:
    """The text of a document's opening pages, or nothing."""
    import pymupdf

    try:
        opened = pymupdf.open(path)
    except Exception:  # noqa: BLE001 - a file that cannot be opened says nothing
        return ""
    try:
        return " ".join(
            (opened[index].get_text() or "") for index in range(min(pages, opened.page_count))
        )
    finally:
        opened.close()


def classify_corpus(
    session: Session,
    *,
    pages: int = 2,
    apply: bool = False,
) -> ClassifyReport:
    """Read every corpus document and fill the columns that are still empty.

    A pass takes each document's file name, its declared model or family and its
    first pages. The role lands on the document; the type lands on the product
    the document points at, from the first one of its documents that names one.
    """
    from app.services.match.match import _corpus_file

    report = ClassifyReport()
    decided: dict[str, tuple[str, str]] = {}
    rows = session.execute(
        select(
            document.c.id,
            document.c.path,
            document.c.doc_type,
            document.c.model_or_family,
            document.c.category,
            document.c.doc_role,
            document.c.product_id,
            product.c.model,
            product.c.product_type,
            product.c.classification_source,
            manufacturer.c.name,
        )
        .join(
            # Outer, because a document with no product is still a document: it
            # is the non-product files this pass has to name.
            product,
            product.c.id == document.c.product_id,
            isouter=True,
        )
        .join(manufacturer, manufacturer.c.id == document.c.manufacturer_id)
        .order_by(document.c.path)
    ).all()

    for (
        document_id,
        stored_path,
        declared_type,
        model_or_family,
        doc_category,
        role,
        product_id,
        product_model,
        product_type,
        product_source,
        maker,
    ) in rows:
        report.documents += 1
        name = Path(str(stored_path or "")).name
        file = _corpus_file(str(stored_path)) if stored_path else None
        body = _first_pages(file, pages) if file and file.exists() else ""
        if not body:
            report.unreadable += 1
        snippet = body[:ROLE_TEXT_CHARS]
        cover = (name, model_or_family)
        words = (*cover, snippet)

        if not role:
            found_role = doc_role(declared_type, *cover, pages=snippet)
            if found_role:
                report.roles += 1
                if apply:
                    session.execute(
                        update(document)
                        .where(document.c.id == document_id)
                        .values(doc_role=found_role)
                    )

        if (
            product_id
            and not product_type
            # A product that already carries a source has been decided, and the
            # decision may have been *no single type*. An answer of blank is a
            # reviewer's answer, and a re-run that filled it again would undo
            # the one correction the CSV exists to make (D-153).
            and not product_source
            and product_id not in decided
        ):
            found_type = document_type_of(doc_category, *words)
            # The opening pages do not overrule the document's own name. A
            # control-valve bulletin's first page routinely describes the ball
            # valve the actuator sits on, and a page-word type then contradicts
            # the cover it was read from (D-149). Where the name already places
            # the product and the text disagrees, the row stays unresolved.
            cover = document_type_of(doc_category, name, model_or_family)
            if cover and cover != found_type:
                found_type = None
            if found_type:
                decided[product_id] = (found_type, name)

        if not product_type and not decided.get(product_id):
            # Unresolved is a state a reviewer has to see, so the row keeps the
            # evidence the rules had: the document and the words around it.
            report.unresolved.append(
                {
                    "manufacturer": maker,
                    "product": product_model,
                    "document": name,
                    "product_type": "",
                    "doc_role": role or doc_role(declared_type, *words) or "",
                    "first_page_snippet": " ".join(snippet.split())[:300],
                }
            )

    report.types = len(decided)
    if apply:
        for product_id, (found_type, deciding) in decided.items():
            session.execute(
                update(product)
                .where(
                    product.c.id == product_id,
                    product.c.product_type.is_(None),
                )
                .values(product_type=found_type, classification_source="rule")
            )
        session.commit()
    return report


def export_unresolved(report: ClassifyReport, csv_path: Path) -> int:
    """Write the rows a reviewer has to decide. Returns how many were written."""
    fieldnames = (
        "manufacturer",
        "product",
        "document",
        "product_type",
        "doc_role",
        "first_page_snippet",
    )
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report.unresolved)
    return len(report.unresolved)


def load_types(session: Session, csv_path: Path) -> int:
    """Apply the labels a reviewer filled, and say who put them there.

    A value from the CSV is a person's decision, so it wins over the rules and
    is recorded as `human`. Blank cells change nothing.
    """
    from app.repositories.corpus import manufacturer_id

    applied = 0
    with csv_path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            found_type = (row.get("product_type") or "").strip()
            role = (row.get("doc_role") or "").strip()
            name = (row.get("manufacturer") or "").strip()
            if found_type and name and (row.get("product") or "").strip():
                applied += session.execute(
                    update(product)
                    .where(
                        product.c.manufacturer_id
                        == manufacturer_id(session, name),
                        product.c.model == row["product"].strip(),
                        product.c.product_type.is_(None),
                    )
                    .values(product_type=found_type, classification_source="human")
                ).rowcount
            if role and (row.get("document") or "").strip():
                # The reviewer names the document; its stored path may carry the
                # directory it was collected in.
                # shortcut: matched by file name, upgrade when two documents in
                # the corpus share one.
                applied += session.execute(
                    update(document)
                    .where(
                        document.c.path.endswith(Path(row["document"]).name),
                        document.c.doc_role.is_(None),
                    )
                    .values(doc_role=role)
                ).rowcount
    session.commit()
    return applied


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    from app.repositories.db import session_scope

    parser = argparse.ArgumentParser(
        description="Declare each corpus document's role and each product's type."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write what the rules decided; without it nothing is changed",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=2,
        help="how many opening pages to read (default 2)",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_TODO_PATH),
        help="where to write the rows the rules could not decide",
    )
    parser.add_argument(
        "--load",
        help="apply a filled CSV and stop there",
    )
    args = parser.parse_args(argv)

    with session_scope() as session:
        if args.load:
            print(f"human labels applied: {load_types(session, Path(args.load))}")
            return 0
        report = classify_corpus(session, pages=args.pages, apply=args.apply)
    print(report.summary())
    written = export_unresolved(report, Path(args.out))
    print(f"unresolved rows written: {written} -> {args.out}")
    if not args.apply:
        print("nothing was written to the database (add --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
