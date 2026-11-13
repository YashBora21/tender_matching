# Migration and retry handoff - 2026-11-13

The user authorizes the second agent to apply the migrations and retry failed
readings. Codex handles source-reading diagnosis and code integration. Keep
these tracks separate: no reset, environment rewrite, staging of unrelated local
work, or corpus deletion is part of this deployment task.

## Already committed

- b633c9c: migration0022 and spend table schema prerequisite.
- a2315c5: migration0023, reading attempts, provenance and read-again.
- 4d700b3: reader17 blank-page fix. Include this before retrying the page failure.

Both migrations are additive; monetary-cap runtime work is separate local work.
Do not create duplicate migrations or change provider credentials.

## Apply and check (PowerShell, repository root)

```powershell
Push-Location backend
try {
    ..\venv\Scripts\python.exe -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Migration failed; do not retry extraction' }
    ..\venv\Scripts\python.exe -m alembic current
    if ($LASTEXITCODE -ne 0) { throw 'Revision check failed' }
} finally { Pop-Location }
```

Read-only verification on2026-11-13 already reports `0023_reading_attempts`.
Verify it locally; no duplicate migration commit is needed. Restart the backend if it
has not loaded this checkout. Check `/api/preflight` for numeric effective limits;
provider identities must stay redacted. Current and historical attempt limits
are separate. Use the UI's **Read this tender again**, or:

```powershell
$TenderId = '<TENDER_ID>'
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/tenders/$TenderId/read-again"
```

A successful reading is reused by ordinary re-upload; explicit read-again
rebuilds it with current limits and valid cached responses. Old requirements and
reports remain. Respect configured call/token budgets and keep matching grouping
off. Do not start duplicate reads already queued or processing.

## Independent page diagnosis

For tender7a1384ad, specification page3 is entirely white with no source text.
The old reader attempted OCR and treated its empty boxes as a failure. Reader17
records the proven blank page as skipped with an explicit reason. The supporting
annexure has text on pages1-2 and an image form on page3 that Paddle reads.
Local reader verification:26 pages accounted for,24 parsed,1 OCR,1 blank skip,
0 failed; no provider calls or database writes. This does not grade structured
requirements or manufacturer matching.

The restricted trial-category attachment remains unresolved. The collector's
robots restriction must stay respected. Supply an authorized local copy through
the attachment-upload control against its recorded source URL; preserve that
manual attachment on retry. Do not mark an absent source as read.

## Reset remains a separate review

Read `OPENROUTER_TRACK_B.md` for the backup command. The current reset preview
selects6 tenders and protects9, including GeM-Bidding-10103860 by audit reference.
The refreshed preview after migration has6 selected tenders,21 attachments,
45 items,1848 requirements,4 runs and73 results. All737 legacy cache entries
are retained; no cache entries would be deleted. Raw preview is in
`.audit_tmp/reset-preview-2026-11-13.txt` locally. Review it before any execution.
No reset is authorized by this migration handoff.
