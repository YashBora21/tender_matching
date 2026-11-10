"""One completion gate shared by ingestion, recovery and reports."""
from collections import Counter


def page_coverage(tender, attachments, pages):
    groups = {}
    for attachment in attachments:
        count = attachment.get('pages')
        if count is None:
            continue
        identity = attachment.get('sha256') or attachment.get('id') or attachment.get('url')
        group = groups.setdefault(identity, {'keys': set(), 'pages': count})
        group['keys'].update(str(value) for value in (attachment.get('id'), attachment.get('sha256')) if value)
    wrapper_hash = tender.get('sha256') or tender.get('wrapper_sha256')
    wrapper_count = tender.get('page_count')
    if wrapper_count and wrapper_hash not in groups:
        groups['wrapper'] = {'keys': {'wrapper'}, 'pages': wrapper_count}
    missing = 0
    sources = []
    for identity, group in groups.items():
        actual = {page['page'] for page in pages if
                  str(page.get('source_key') or page.get('attachment_id') or 'legacy') in group['keys']}
        if not actual and len(groups) == 1:
            actual = {page['page'] for page in pages if not page.get('attachment_id') and
                      page.get('source_key', 'legacy') == 'legacy'}
        absent = sorted(set(range(1, group['pages'] + 1)) - actual)
        missing += len(absent)
        sources.append({'source': str(identity), 'declared': group['pages'], 'missing_pages': absent})
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
            'incomplete_ingestion': bool(unresolved or missing)}


def completion_gaps(tender, attachments, pages, requirements):
    gaps = []
    if page_coverage(tender, attachments, pages)['incomplete_ingestion']:
        gaps.append('source pages are failed, unaccounted for, or have invalid skip records')
    if any(attachment.get('status') in {'failed', 'restricted'} for attachment in attachments):
        gaps.append('a source attachment is unresolved')
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
