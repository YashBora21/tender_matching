"""What each tender's requirements look like, by type and by how they were read.

Read-only: it opens the database, counts, and prints. It never writes, never
calls a model and never changes a tender.

The point of the audit is to tell a *fragment* or a *mislabelled technical
clause* apart from a clause a document genuinely cannot answer. `unknown` is
the bucket both end up in, so the audit measures how much of it looks
technical to a cheap scan:

    python -m scripts.extraction_audit                     # DATABASE_URL from .env
    python -m scripts.extraction_audit --database-url ...  # another database

The "technical-looking" column is an upper bound, not a verdict: it counts a
number with a unit, or a specification keyword, anywhere in the clause.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

import psycopg  # noqa: E402

# A number with a unit is the strongest cheap signal that a clause states a
# value. The units are the ones the tenders in this corpus use.
NUMBER_WITH_UNIT = re.compile(
    r"\d+(?:[.,]\d+)?\s*"
    r"(?:mm|cm|mtr|meter|metre|m\b|inch|in\b|\"|kg|gm|g\b|ton|mt\b|"
    r"bar|psi|pa|kpa|mpa|kg/cm2|kg/cm²|"
    r"kw|kwh|hp|watt|w\b|volt|v\b|amp|a\b|hz|rpm|"
    r"lpm|lps|m3/h|m³/h|m3/hr|gpm|"
    r"deg|c\b|°c|%|percent)",
    re.IGNORECASE,
)

# The words a specification uses for something a document can state.
SPEC_KEYWORD = re.compile(
    r"\b(?:size|diameter|bore|head|flow|capacity|discharge|pressure|rating|"
    r"class|material|thickness|efficiency|power|speed|voltage|frequency|"
    r"temperature|weight|length|width|height|grade|standard|test)\b",
    re.IGNORECASE,
)

# The columns this audit prints. Kept together so the header and the rows
# cannot drift apart.
COLUMNS = (
    ("tender", 46),
    ("status", 12),
    ("pages", 11),
    ("reqs", 5),
    ("tech", 5),
    ("cert", 5),
    ("comm", 5),
    ("elig", 5),
    ("info", 5),
    ("unk", 5),
    ("unk%", 6),
    ("<25", 5),
    ("looks tech", 10),
    ("method", 26),
)


@dataclass
class TenderCounts:
    tender: str
    status: str
    pages_logged: int
    page_count: int
    types: dict[str, int] = field(default_factory=dict)
    unknown_short: int = 0
    unknown_technical: int = 0
    methods: dict[str, int] = field(default_factory=dict)

    @property
    def requirements(self) -> int:
        return sum(self.types.values())

    @property
    def unknown(self) -> int:
        return self.types.get("unknown", 0)


def _dsn(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


def _counts(cur, tender_id: str) -> TenderCounts:
    cur.execute("select filename, status from tender where id = %s", (tender_id,))
    filename, status = cur.fetchone()

    # The ledger describes the specification, whose page count is not the
    # wrapper's. A ledger of 22 pages for a 7-page wrapper is right, not an
    # over-read (D-91): the declared count is the document that was read.
    cur.execute(
        """
        select coalesce(
            (
                select a.pages
                from tender_attachment a
                where a.id = (
                    select p.attachment_id
                    from tender_page p
                    where p.tender_id = %(tender)s and p.attachment_id is not null
                    limit 1
                )
            ),
            (select t.page_count from tender t where t.id = %(tender)s)
        )
        """,
        {"tender": tender_id},
    )
    page_count = cur.fetchone()[0]

    cur.execute(
        """
        select type, count(*)
        from requirement
        where tender_id = %s
        group by type
        """,
        (tender_id,),
    )
    types = {row[0]: row[1] for row in cur.fetchall()}

    cur.execute(
        """
        select coalesce(text, source_text, '') as clause
        from requirement
        where tender_id = %s and type = 'unknown'
        """,
        (tender_id,),
    )
    clauses = [row[0] for row in cur.fetchall()]
    short = sum(1 for clause in clauses if len(clause.strip()) < 25)
    technical = sum(
        1
        for clause in clauses
        if NUMBER_WITH_UNIT.search(clause) or SPEC_KEYWORD.search(clause)
    )

    cur.execute(
        """
        select extraction_method, count(*)
        from requirement
        where tender_id = %s
        group by extraction_method
        """,
        (tender_id,),
    )
    methods = {row[0]: row[1] for row in cur.fetchall()}

    cur.execute("select count(*) from tender_page where tender_id = %s", (tender_id,))
    pages_logged = cur.fetchone()[0]

    return TenderCounts(
        tender=filename,
        status=status,
        pages_logged=pages_logged,
        page_count=page_count or 0,
        types=types,
        unknown_short=short,
        unknown_technical=technical,
        methods=methods,
    )


def audit(database_url: str) -> list[TenderCounts]:
    with psycopg.connect(_dsn(database_url)) as conn, conn.cursor() as cur:
        cur.execute(
            "select id from tender order by uploaded_at nulls last, filename"
        )
        ids = [row[0] for row in cur.fetchall()]
        return [_counts(cur, tender_id) for tender_id in ids]


def _row(counts: TenderCounts) -> str:
    percent = (
        f"{100.0 * counts.unknown / counts.requirements:.0f}%"
        if counts.requirements
        else "-"
    )
    methods = " ".join(f"{name}={n}" for name, n in sorted(counts.methods.items()))
    values = (
        counts.tender[:44],
        counts.status[:11],
        f"{counts.pages_logged}/{counts.page_count}",
        str(counts.requirements),
        str(counts.types.get("technical", 0)),
        str(counts.types.get("certification", 0)),
        str(counts.types.get("commercial", 0)),
        str(counts.types.get("eligibility", 0)),
        str(counts.types.get("informational", 0)),
        str(counts.unknown),
        percent,
        str(counts.unknown_short),
        str(counts.unknown_technical),
        methods,
    )
    return " ".join(
        f"{value:<{width}}" for value, (_, width) in zip(values, COLUMNS)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit a tender database's requirements.")
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL; defaults to DATABASE_URL from the environment or .env",
    )
    args = parser.parse_args(argv)

    url = args.database_url
    if not url:
        from app.core.config import get_settings

        url = get_settings().database_url

    rows = audit(url)
    print(" ".join(f"{name:<{width}}" for name, width in COLUMNS))
    print("-" * (sum(width for _, width in COLUMNS) + len(COLUMNS) - 1))
    for counts in rows:
        print(_row(counts))

    total = sum(counts.requirements for counts in rows)
    unknown = sum(counts.unknown for counts in rows)
    print()
    print(
        f"{len(rows)} tenders, {total} requirements, {unknown} unknown "
        f"({100.0 * unknown / total:.0f}% of requirements)" if total else "no requirements"
    )
    print(
        "unknown with a number+unit or a spec keyword: "
        f"{sum(counts.unknown_technical for counts in rows)} "
        "(an upper bound on mislabelled technical clauses)"
    )
    print(
        "unknown shorter than 25 characters: "
        f"{sum(counts.unknown_short for counts in rows)} (fragments)"
    )
    print(
        "tenders with 0 pages logged: "
        f"{sum(1 for counts in rows if counts.pages_logged == 0)} of {len(rows)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
