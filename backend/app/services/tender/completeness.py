"""One completion gate shared by ingestion, recovery and reports."""
from collections import Counter
from app.services.tender.source_policy import blocking_requirement, policy, source_role


def page_coverage(tender, attachments, pages):
    groups = {}
    for attachment in attachments:
        count = attachment.get('pages')
        if count is None:
            continue
        identity = attachment.get('sha256') or attachment.get('id') or attachment.get('url')
        group = groups.setdefault(identity, {'keys': set(), 'pages': count, 'source_role': source_role(attachment)})
        group['keys'].update(str(value) for value in (attachment.get('id'), attachment.get('sha256')) if value)
    wrapper_hash = tender.get('sha256') or tender.get('wrapper_sha256')
    wrapper_count = tender.get('page_count')
    if wrapper_count and wrapper_hash not in groups:
        groups['wrapper'] = {'keys': {'wrapper'}, 'pages': wrapper_count, 'source_role': 'cover'}
    missing = 0
    sources = []
    page_role = {}
    for identity, group in groups.items():
        member = [page for page in pages if
                  str(page.get('source_key') or page.get('attachment_id') or 'legacy') in group['keys']]
        if not member and len(groups) == 1:
            member = [page for page in pages if not page.get('attachment_id') and
                      page.get('source_key', 'legacy') == 'legacy']
        actual = {page['page'] for page in member}
        absent = sorted(set(range(1, group['pages'] + 1)) - actual)
        missing += len(absent)
        roles = {tag.split(':', 1)[1] for page in member
                 for tag in page.get('tags') or [] if tag.startswith('source_role:')}
        role = next(iter(roles)) if len(roles) == 1 else group['source_role']
        for page in member:
            page_role[id(page)] = role
        read = sum(1 for page in member if page.get('ledger_status') in {'parsed', 'ocr'})
        bad_pages = [page['page'] for page in member
                     if page.get('ledger_status') not in {'parsed', 'ocr', 'skipped'}
                     or (page.get('ledger_status') == 'skipped' and not page.get('skip_reason'))]
        sources.append({'source': str(identity), 'declared': group['pages'], 'missing_pages': absent,
                        'source_role': role, 'read': read,
                        'unread_pages': sorted(set(absent) | set(bad_pages))})
    unresolved, skipped = [], []
    for page in pages:
        status = page.get('ledger_status')
        entry = {'page': page['page']}
        if page.get('source_key'):
            entry['source_key'] = page['source_key']
        if status not in {'parsed', 'ocr', 'skipped'} or (status == 'skipped' and not page.get('skip_reason')):
            unresolved.append(entry)
        if status == 'skipped':
            skipped.append({**entry, 'reason': page.get('skip_reason')})
    return {'declared': sum(group['pages'] for group in groups.values()) if groups else wrapper_count,
            'accounted': len(pages), 'unaccounted': missing,
            'counts': dict(Counter(page.get('ledger_status') or '' for page in pages)),
            'unresolved': unresolved, 'skipped': skipped, 'sources': sources,
            'incomplete_ingestion': bool(unresolved or missing),
            'primary_incomplete': _primary_incomplete(sources, pages, page_role)}


def _primary_incomplete(sources, pages, page_role):
    """Specification/BOQ pages block; cover, ATC and SLA never do (D-190).

    An unattributed page (a legacy single-source tender with no attachment
    records) is conservatively primary, so a failed page there still blocks.
    """
    roles = policy()
    if any(s['unread_pages'] for s in sources if s['source_role'] in roles['primary_roles']):
        return True
    for page in pages:
        if id(page) in page_role:  # attributed; its source already decided
            continue
        status = page.get('ledger_status')
        if status not in {'parsed', 'ocr', 'skipped'} or (status == 'skipped' and not page.get('skip_reason')):
            return True
    return False


def _primary_sources(coverage):
    primary = policy()['primary_roles']
    return [s for s in coverage.get('sources', []) if s.get('source_role') in primary]


def primary_incomplete(coverage):
    """Only specification/BOQ pages can mark a reading incomplete (D-190).

    Cover, ATC and SLA pages never block a run; the all-pages
    `incomplete_ingestion` label stays honest for the report banner.
    """
    return coverage.get('primary_incomplete', False)


def reading_coverage(coverage):
    """Specification & BOQ pages read / declared, the partial-reading figure.

    Page granularity is what the ledger stores; there is no per-line ledger,
    so this honestly measures how much of the primary source was read (D-190).
    """
    primary = _primary_sources(coverage)
    total = sum(s['declared'] for s in primary)
    represented = sum(s.get('read', 0) for s in primary)
    return {'represented': represented, 'total': total,
            'percent': round(100.0 * represented / total, 1) if total else 0.0,
            'basis': 'specification & BOQ pages read'}


def completion_gaps(tender, attachments, pages, requirements):
    gaps = []
    coverage = page_coverage(tender, attachments, pages)
    if primary_incomplete(coverage):
        gaps.append('specification or BOQ pages are failed, unaccounted for, or have invalid skip records')
    if any(attachment.get('status') in {'failed', 'restricted'}
           and source_role(attachment) in policy()['primary_roles'] for attachment in attachments):
        gaps.append('a specification or BOQ attachment is unresolved')
    requirements = [row for row in requirements if blocking_requirement(row)]
    if requirements and all(row.get('type', row.get('requirement_type')) == 'unknown' for row in requirements):
        gaps.append('all extracted clauses have unresolved requirement types')
    unresolved = [row for row in requirements if
                  row.get('type', row.get('requirement_type')) == 'unknown' and
                  not (row.get('review_reason') or '').startswith('page classified as')]
    from app.services.requirement.coverage import MARKER
    if any(MARKER in (row.get('review_reason') or '') for row in requirements):
        gaps.append('source clauses contain content not represented by structured requirements')
    if unresolved:
        gaps.append('some source clauses could not be interpreted or assigned')
    technical = [row for row in requirements if row.get('type', row.get('requirement_type')) in {'technical', 'certification'}]
    if any(not row.get('item_id') for row in technical):
        gaps.append('technical clauses have unresolved BOQ item assignments')
    if technical and not any(row.get('attribute') for row in technical):
        gaps.append('technical clauses have no extracted attributes; source interpretation is incomplete')
    return gaps
