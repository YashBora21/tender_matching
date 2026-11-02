/** Pure views over a report: labels, counts and filters.
 *
 * Kept out of the components so the rules a reader trusts - what a status is
 * called, which rows a filter keeps - can be tested without a browser.
 */

import type { Assessment, Report, ReviewEntry } from './types'

const LABELS: Record<string, string> = {
  full_match: 'Full match',
  partial_match: 'Partial match',
  missing_capability: 'Missing capability',
  unsupported_requirement: 'Unsupported requirement',
  no_evidence_found: 'No evidence found',
  passed: 'Passed',
  failed: 'Failed',
  family_level: 'Family level',
  conflicting: 'Conflicting',
  not_checked: 'Not checked',
  model_level: 'Model level',
  unknown: 'Unknown',
  queued: 'Queued',
  processing: 'Processing',
  parsing: 'Parsing',
  retrieving: 'Retrieving',
  verifying: 'Verifying',
  scoring: 'Scoring',
  done: 'Done',
  // Both endings of a read or a run that stopped short of answering everything.
  rate_limited: 'Rate limited',
  incomplete: 'Incomplete',
  parsed: 'Parsed',
  skipped: 'Skipped',
  ocr: 'OCR',
  high: 'High',
  medium: 'Medium',
  low: 'Low',
}

/** A vocabulary word as a reader expects it, without inventing a meaning. */
export function label(value: string | null | undefined): string {
  if (!value) return '—'
  return LABELS[value] ?? value.replaceAll('_', ' ')
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const rounded = Math.round(value * 10) / 10
  return `${rounded}%`
}

/** A stored 0..1 score as a percentage; no score is not zero. */
export function formatScore(score: number | null | undefined): string {
  if (score === null || score === undefined) return '—'
  return `${Math.round(score * 100)}%`
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

/** Every requirement × manufacturer, unsupported clauses included.
 *
 * The report keeps unsupported clauses in their own list because they are
 * excluded from the score, but they are still rows a reviewer asks about, so
 * the table shows them, labelled, rather than hiding them.
 */
export function assessments(report: Report): Assessment[] {
  const supported: Assessment[] = report.requirements.flatMap((requirement) =>
    requirement.results.map((result) => ({ requirement, result, unsupported: false })),
  )
  const unsupported: Assessment[] = report.unsupported_requirements.flatMap(
    (requirement) =>
      requirement.results.map((result) => ({ requirement, result, unsupported: true })),
  )
  return [...supported, ...unsupported]
}

export function countStatuses(rows: Assessment[]): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const row of rows) {
    counts[row.result.status] = (counts[row.result.status] ?? 0) + 1
  }
  return counts
}

export interface Filters {
  status: string | null
  manufacturer: string | null
  reviewOnly: boolean
}

export const NO_FILTERS: Filters = {
  status: null,
  manufacturer: null,
  reviewOnly: false,
}

export function filterAssessments(
  rows: Assessment[],
  filters: Filters,
): Assessment[] {
  return rows.filter((row) => {
    if (filters.status && row.result.status !== filters.status) return false
    if (filters.manufacturer && row.result.manufacturer !== filters.manufacturer) {
      return false
    }
    if (filters.reviewOnly && !row.result.review_required) return false
    return true
  })
}

export function requirementText(
  report: Report,
  entry: ReviewEntry,
): string | null {
  const found = [...report.requirements, ...report.unsupported_requirements].find(
    (requirement) => requirement.requirement_id === entry.requirement_id,
  )
  return found?.text ?? null
}

/** The statuses present in a report, in the PRD's order, for the filter bar. */
const STATUS_ORDER = [
  'full_match',
  'partial_match',
  'missing_capability',
  'unsupported_requirement',
  'no_evidence_found',
]

export function statusesPresent(counts: Record<string, number>): string[] {
  const known = STATUS_ORDER.filter((status) => (counts[status] ?? 0) > 0)
  const rest = Object.keys(counts)
    .filter((status) => !STATUS_ORDER.includes(status) && counts[status] > 0)
    .sort()
  return [...known, ...rest]
}
