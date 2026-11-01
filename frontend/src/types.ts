/** The shapes the backend answers with (PRD 11).
 *
 * Only the fields the screens read are declared. An extra field in a response
 * is not an error; a missing one the UI assumes is.
 */

export type TenderStatus = 'queued' | 'processing' | 'done' | 'failed' | 'rate_limited'

export interface TenderUpload {
  tender_id: string
  status: TenderStatus
  duplicate: boolean
}

export interface RequirementRow {
  id: string
  item_id: string | null
  text: string
  source_text: string | null
  page: number
  type: string
  attribute: string | null
  review_required: boolean
  review_reason: string | null
  value: string | null
  unit: string | null
  mandatory: boolean
  item_title?: string | null
  category?: string | null
}

export interface TenderRequirements {
  tender_id: string
  filename: string | null
  status: TenderStatus
  error: string | null
  page_count: number | null
  /** True while the tender is still being read, so the list is what has been
   * read so far and not the whole document (D-82). */
  partial?: boolean
  counts: { items: number; requirements: number; flagged: number }
  requirements: RequirementRow[]
}

/** One document a tender links to, as the manifest records it.
 *
 * `failed` and `restricted` are the two states the reviewer can fix by hand:
 * the file exists, the fetch could not get it. */
export interface AttachmentRow {
  url: string | null
  name: string | null
  sha256: string | null
  kind: string | null
  status: string | null
  bytes: number | null
  pages: number | null
  retrieved_at: string | null
  note: string | null
}

export interface TenderAttachments {
  tender_id: string
  category: string | null
  attachments: AttachmentRow[]
  /** The URLs of the attachments that could not be fetched: what to upload. */
  missing: (string | null)[]
}

/** What the backend says about a file it accepted in place of a failed
 * fetch. The record is written with `manual`, because a person supplied it. */
export interface AttachmentUpload {
  tender_id: string
  name: string
  url: string
  sha256: string
  kind: string | null
  bytes: number
  pages: number | null
  status: string
}

export interface ManufacturerRow {
  id: string
  name: string
  documents: number
  products: number
  chunks: number
  categories: string[]
}

export interface Manufacturers {
  count: number
  totals: { documents: number; products: number; chunks: number }
  categories: string[]
  corpus_hash: string
  manufacturers: ManufacturerRow[]
}

export interface RunProgress {
  run_id: string
  tender_id: string
  status: string
  manufacturers: string[]
  requirements: number
  expected_results: number
  results: number
  progress_percent: number
  error: string | null
}

export interface Evidence {
  fact_id: string
  page: number | null
  quote: string | null
  document_id: string | null
  document: string | null
  doc_type: string | null
  sha256: string | null
  url: string | null
}

export interface ResultRow {
  manufacturer_id: string
  manufacturer: string
  status: string
  score: number | null
  verification_outcome: string
  applicability: string
  capability_text: string | null
  confidence: string | null
  review_required: boolean
  review_reason: string | null
  evidence: Evidence | null
}

export interface ReportRequirement {
  requirement_id: string
  item_id: string | null
  item_title: string | null
  category: string | null
  text: string
  source_text: string | null
  type: string
  mandatory: boolean
  page: number | null
  value: string | null
  unit: string | null
  results: ResultRow[]
}

export interface ManufacturerSummary {
  manufacturer_id: string
  manufacturer: string
  rank: number
  compatibility_percent: number
  coverage_percent: number
  status_counts: Record<string, number>
  fails_mandatory: boolean
  review_count: number
}

export interface PagesLedger {
  declared: number | null
  accounted: number
  unaccounted: number
  counts: Record<string, number>
  unresolved: { page: number }[]
  skipped: { page: number; reason: string | null }[]
  incomplete_ingestion: boolean
}

export interface ReviewEntry {
  requirement_id: string
  manufacturer: string
  status: string
  reason: string | null
}

export interface Report {
  run: {
    id: string
    tender_id: string
    status: string
    started_at: string | null
    error: string | null
    manufacturers: string[]
    corpus_hash: string | null
  }
  tender: {
    id: string | null
    filename: string | null
    sha256: string | null
    page_count: number | null
    pages: PagesLedger
    extraction?: { status: string; detail: string | null; complete: boolean }
  }
  /** True when the tender was not read in full, by pages or by extraction. A
   * report with this set is not a final compatibility report (D16). */
  incomplete_ingestion?: boolean
  config: Record<string, unknown>
  results: { stored: number; expected: number; complete: boolean }
  manufacturers: ManufacturerSummary[]
  requirements: ReportRequirement[]
  unsupported_requirements: ReportRequirement[]
  review_queue: ReviewEntry[]
  status_meanings: Record<string, string>
}

/** One requirement assessed against one manufacturer: the unit the table and
 * every filter work on. Unsupported clauses are assessments too; they are
 * simply excluded from the score. */
export interface Assessment {
  requirement: ReportRequirement
  result: ResultRow
  unsupported: boolean
}
