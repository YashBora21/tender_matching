"""The corpus catalogue: the manufacturers and the products.

The corpus is a set of documents. A fact points at the product it is about,
so the products have to exist before anything can be matched.

The rules, which are not negotiable:

- **A model is never invented.** It comes from the manifest or from the
  document itself, and from nowhere else.
- **A document with no model gets one family-level row**, named after the
  document's own title, and it is marked family-level. A fact about a
  catalogue is family-level evidence: uncertain applicability, capped and
  flagged for review (PRD 4).
- **The seeding is idempotent.** A product is found on the manufacturer and
  the model, so a re-run adds nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.repositories.schema import document, fact, manufacturer, product

logger = get_logger(__name__)


@dataclass
class SeedReport:
    """What the seeding did, and what it could not do."""

    products: int = 0
    family_level: int = 0
    # The documents that named no model, so a family-level row was made for
    # them. They are listed for a reviewer to look at.
    family_level_documents: list[str] = field(default_factory=list)
    # The documents with no title either: nothing could be made for them.
    unnamed_documents: list[str] = field(default_factory=list)
    per_manufacturer: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        """The summary a reviewer reads before anything is extracted."""
        lines = ["", "PRODUCTS PER MANUFACTURER"]
        for name, count in sorted(self.per_manufacturer.items()):
            lines.append(f"  {name:24} {count}")
        lines.append("")
        lines.append(f"  products created        {self.products}")
        lines.append(f"  family-level products   {self.family_level}")
        lines.append(f"  documents with no model {len(self.family_level_documents)}")
        lines.append(f"  documents with no title {len(self.unnamed_documents)}")
        if self.family_level_documents:
            lines.append("")
            lines.append("FAMILY-LEVEL DOCUMENTS (a row was made for each)")
            for name in self.family_level_documents:
                lines.append(f"  {name}")
        if self.unnamed_documents:
            lines.append("")
            lines.append("DOCUMENTS WITH NO MODEL AND NO TITLE (nothing was made)")
            for name in self.unnamed_documents:
                lines.append(f"  {name}")
        return "\n".join(lines)


# The series a document covers, as its own title states it:
# "1671729175-Technical-Bulletin---113-PRODUCT-SPECIFICATION-SERIES-75-76" is
# the series 75/76, and "1659634039-Final-Series-51-53-v5b_04-08-2022" is the
# series 51/53.
_SERIES_IN_TITLE = re.compile(
    r"series[-_ ]?([A-Z0-9]{1,5}(?:\s*[-/,]\s*[A-Z0-9]{1,5})*)", re.IGNORECASE
)

# A part that is a year is a date the title happens to carry, not a series
# number: "Final-Series-58-2022" is the series 58 and a date.
_YEAR = re.compile(r"^(?:19|20)\d{2}$")

# A part that is a version, not a series: "Series-51-53-v5b" is the series
# 51/53 at version 5b.
_VERSION = re.compile(r"^[vr]\d+[a-z]?$", re.IGNORECASE)

# A part that names a series: a number, or a code built from one. A word
# that follows "series" in a title is a word, not a series: "Series 83 Valve
# Position Monitor" is the series 83 and a valve position monitor.
_SERIES_PART = re.compile(r"^(?:\d{1,3}[A-Za-z]{0,2}|[A-Za-z]\d{1,3}[A-Za-z]?)$")


def _title_of(path: Optional[str]) -> Optional[str]:
    """The title of a document, read from its file name.

    A catalogue is named after the product or the series it covers, so the
    name is the title. It is not a model: a document that names no model gets
    a family-level row, and the title is only the family's name.
    """
    if not path:
        return None
    stem = Path(path).stem
    if not stem or stem.lower() in ("raw", "document", "index"):
        return None
    return stem[:255]


def _family_of(path: Optional[str]) -> Optional[str]:
    """The family a document covers, from its own title.

    The title names the series when it names one, and the series is the
    family. A title that names no series is the family's name on its own.
    """
    title = _title_of(path)
    if not title:
        return None
    match = _SERIES_IN_TITLE.search(title)
    if not match:
        return title
    parts = [part.strip() for part in re.split(r"\s*[-/,]\s*", match.group(1))]
    # A year is a date the title happens to carry, not a series number, and
    # everything from it on is the date: "Series-58-2022-M2" is the series 58.
    for index, part in enumerate(parts):
        if _YEAR.match(part):
            parts = parts[:index]
            break
    # A version is not a series either: "Series-51-53-v5b" is the series
    # 51/53 at version 5b.
    while parts and _VERSION.match(parts[-1]):
        parts.pop()
    # Only the parts that name a series: the words after them are a title.
    parts = [part for part in parts if _SERIES_PART.match(part)]
    if not parts:
        return title
    return "Series " + "/".join(parts)


# The document types the catalogue manifest uses, and the role the
# classifier found. A bill of quantities catalogues the items, so it is a
# catalogue; a specification states the requirements.
_DOC_TYPE_BY_ROLE = {
    "boq-csv": "catalogue",
    "boq-pdf": "catalogue",
    "spec": "specification",
    "form": "other",
    "scanned": "other",
    "other": "other",
}


def complete_catalogue_manifest(catalogue_dir: Path) -> int:
    """Add the documents on disk that the catalogue manifest does not list.

    A corpus gathered by hand has files that no manifest names, and a
    manifest that does not list them is a manifest that is behind: the
    corpus is the set of files that exist.

    The columns that can be read are filled in - the hash, the size, the page
    count, the type - and the columns that cannot are left empty. A source
    URL that was never recorded stays empty rather than being invented.
    """
    import csv
    from datetime import datetime, timezone

    from app.services.tender.collect import classify_document, sha256_file

    catalogue_dir = Path(catalogue_dir)
    manifest_path = catalogue_dir / "manifest.csv"
    if not manifest_path.exists():
        return 0

    with manifest_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fields = list(rows[0].keys()) if rows else [
            "manufacturer",
            "filename",
            "source_url",
            "retrieved_at",
            "sha256",
            "bytes",
            "pages",
            "doc_type",
            "notes",
        ]

    listed = {(row.get("manufacturer"), row.get("filename")) for row in rows}

    added: list[dict] = []
    for directory in sorted(catalogue_dir.iterdir()):
        if not directory.is_dir():
            continue
        for file_path in sorted(directory.glob("*.pdf")):
            if (directory.name, file_path.name) in listed:
                continue
            data = file_path.read_bytes()
            role, pages = classify_document(data, file_path.name)
            try:
                modified = datetime.fromtimestamp(
                    file_path.stat().st_mtime, timezone.utc
                ).isoformat(timespec="seconds")
            except OSError:
                modified = ""
            added.append(
                {
                    "manufacturer": directory.name,
                    "filename": file_path.name,
                    # The source URL was never recorded for this document.
                    "source_url": "",
                    # When the copy on disk was made. It is not the date the
                    # document was downloaded, and it does not claim to be.
                    "retrieved_at": modified,
                    "sha256": sha256_file(file_path),
                    "bytes": str(len(data)),
                    "pages": str(pages or ""),
                    "doc_type": _DOC_TYPE_BY_ROLE.get(role, "other"),
                    "notes": "added from the corpus directory; the source URL was not recorded",
                }
            )

    if not added:
        return 0

    with manifest_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows + added:
            writer.writerow({field: row.get(field, "") for field in fields})
    return len(added)


def seed_corpus(session: Session) -> SeedReport:
    """Make sure every document in the database has a product.

    The documents are already there - the corpus ingestion put them there -
    and the manufacturers with them. What is missing is the product: a fact
    points at the product it is about.
    """
    report = SeedReport()

    rows = session.execute(
        select(
            document.c.id,
            document.c.manufacturer_id,
            document.c.path,
            document.c.model_or_family,
            document.c.category,
        )
    ).all()

    for document_id, manufacturer_id, path, model, category in rows:
        name = _manufacturer_name(session, manufacturer_id)
        if model:
            # The document names a model. It is used as it stands.
            product_id, created = _upsert_product(
                session, manufacturer_id, model, model, category, False
            )
            _link_document(session, document_id, product_id)
            if created:
                report.products += 1
            report.per_manufacturer[name] = report.per_manufacturer.get(name, 0) + 1
            continue

        # No model. A family-level row, named after the document's own title.
        title = _family_of(path)
        if not title:
            report.unnamed_documents.append(f"{name}: {path or '?'}")
            continue
        product_id, created = _upsert_product(
            session, manufacturer_id, title, title, category, True
        )
        _link_document(session, document_id, product_id)
        if created:
            report.products += 1
            report.family_level += 1
            report.family_level_documents.append(f"{name}: {title}")
        report.per_manufacturer[name] = report.per_manufacturer.get(name, 0) + 1

    session.commit()
    return report


def _link_document(session: Session, document_id, product_id: Optional[str]) -> None:
    """Point a document at the product it is about.

    The link is what the retrieval joins on: a document is evidence about one
    product, and joining it to every product of the manufacturer would read
    the same passage once per product, under a name that is not the one the
    document is about.
    """
    if not product_id:
        return
    session.execute(
        update(document)
        .where(document.c.id == document_id)
        .values(product_id=product_id)
    )


def _manufacturer_name(session: Session, manufacturer_id) -> str:
    row = session.execute(
        select(manufacturer.c.name).where(manufacturer.c.id == manufacturer_id)
    ).first()
    return row[0] if row else "?"


def remove_stale_products(session: Session) -> int:
    """Remove the family-level products that no document maps to any more.

    A product is a family, and a document maps to one family. A product that
    no document names is left over from an earlier run, when the family was
    read differently, and the two must not both be there: a fact would point
    at a family nothing documents.

    Only products that no fact points at are removed: a fact references a
    product, and a product that a fact uses is not a leftover.
    """
    wanted: set[tuple] = set()
    for manufacturer_id, path in session.execute(
        select(document.c.manufacturer_id, document.c.path)
    ).all():
        family = _family_of(path)
        if family:
            wanted.add((str(manufacturer_id), family))

    used = {
        row[0]
        for row in session.execute(select(fact.c.product_id).distinct()).all()
        if row[0]
    }
    # A product a document points at is not a leftover either: the document is
    # its evidence.
    used |= {
        row[0]
        for row in session.execute(
            select(document.c.product_id).distinct()
        ).all()
        if row[0]
    }

    removed = 0
    for product_id, manufacturer_id, model in session.execute(
        select(product.c.id, product.c.manufacturer_id, product.c.model).where(
            product.c.is_family_level.is_(True)
        )
    ).all():
        if (str(manufacturer_id), model) in wanted:
            continue
        if str(product_id) in used:
            continue
        session.execute(delete(product).where(product.c.id == product_id))
        removed += 1
    return removed


def _upsert_product(
    session: Session,
    manufacturer_id,
    model: str,
    family: str,
    category: Optional[str],
    is_family_level: bool,
) -> tuple[Optional[str], bool]:
    """Add the product if it is not there.

    Returns the product's id and whether a row was added.
    """
    existing = session.execute(
        select(product.c.id, product.c.family, product.c.is_family_level).where(
            product.c.manufacturer_id == manufacturer_id,
            product.c.model == model,
        )
    ).first()

    if existing:
        # A product that is already there is left alone, so a reviewer's
        # decision is not overwritten. The family and the marker are filled
        # in when they are missing.
        if not existing[1] or not existing[2]:
            from sqlalchemy import update

            session.execute(
                update(product)
                .where(product.c.id == existing[0])
                .values(
                    family=existing[1] or family,
                    is_family_level=bool(existing[2]) or is_family_level,
                )
            )
        return str(existing[0]), False

    import uuid

    product_id = uuid.uuid4()
    session.execute(
        product.insert().values(
            id=product_id,
            manufacturer_id=manufacturer_id,
            model=model,
            family=family,
            category=category,
            is_family_level=is_family_level,
        )
    )
    return str(product_id), True
