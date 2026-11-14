# Part A:read-only cache recovery

325 rows inspected:252 empty nonclaims,73 actual claims.86 PDF source pages
examined. No fully eligible repair;17 existing verified rows are proposed for
review,not changed.197 source-label groups await mapping review.133 relevant
tests passed. Zero database writes,zero LLM calls,no approved aliases added.

Detailed local review artifacts:.audit_tmp/part-a-final-review-20261114/.
Raw evidence is intentionally excluded from this public aggregate report.

Run:python eval/reassess_source_context.py --output-dir NEW_PRIVATE_DIRECTORY
or:python -m app.services.cli.fact_cache --output-dir NEW_PRIVATE_DIRECTORY
(with PYTHONPATH=backend). Existing output directories are never overwritten.
The legacy apply script is guarded and does not apply these new proposals.
