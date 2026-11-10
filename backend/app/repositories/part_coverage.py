"""Cheap indexed evidence checks for explicit source part numbers, never capability verdicts."""
from __future__ import annotations

from sqlalchemy import text


def indexed_part_manufacturers(session, part_number):
    key = ' '.join(part_number.upper().split())
    compact = ''.join(part_number.upper().split())
    cache = session.info.setdefault('part_catalogue_evidence', {})
    if key not in cache:
        rows = session.execute(text(
            "SELECT DISTINCT m.name,c.text FROM chunk c "
            "JOIN document d ON d.id=c.document_id "
            "JOIN manufacturer m ON m.id=d.manufacturer_id "
            "WHERE c.search_vector @@ plainto_tsquery('english', :part)"
        ), {'part': part_number}).all()
        cache[key] = sorted({name for name, passage in rows
                             if compact in ''.join(passage.upper().split())})
    return cache[key]


def source_part(session, requirement):
    identifier = requirement.get('source_table_id')
    row_id = requirement.get('source_row_id')
    if not identifier or row_id is None:
        return None
    from app.repositories.schema import tender_table
    from sqlalchemy import select
    cache = session.info.setdefault('source_table_parts', {})
    if identifier not in cache:
        rows = session.execute(select(tender_table.c.rows).where(tender_table.c.id == identifier)).scalar_one_or_none()
        cache[identifier] = {str(row['row_id']): row.get('part_number') for row in (rows or [])}
    return cache[identifier].get(str(row_id))
