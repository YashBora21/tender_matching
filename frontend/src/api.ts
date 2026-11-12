/** The backend client (PRD 11).
 *
 * The API is the only thing that decides: this module sends what the caller
 * typed and returns what the answer says. Nothing here invents a status, a
 * score or an evidence quote.
 */

import type {
  AttachmentUpload,
  Manufacturers,
  Report,
  RunProgress,
  TenderAttachments,
  TenderRequirements,
  TenderUpload,
} from './types'

const BASE = (import.meta.env.VITE_API_URL ?? 'http://localhost:8000').replace(/\/+$/, '')

export class ApiError extends Error {
  readonly status: number
  readonly code: string | null

  constructor(status: number, message: string, code: string | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, init)
  } catch {
    throw new ApiError(
      0,
      `Cannot reach the backend at ${BASE}. Is it running?`,
    )
  }

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    let code: string | null = null
    try {
      const payload = (await response.json()) as {
        code?: string
        message?: string
        detail?: unknown
      }
      code = payload.code ?? null
      if (payload.message) message = payload.message
      else if (typeof payload.detail === 'string') message = payload.detail
    } catch {
      // A non-JSON error body is not more informative than the status line.
    }
    throw new ApiError(response.status, message, code)
  }

  return (await response.json()) as T
}

export function uploadTender(file: File): Promise<TenderUpload> {
  const body = new FormData()
  body.append('file', file)
  // No Content-Type: the browser sets the multipart boundary.
  return request<TenderUpload>('/api/tenders', { method: 'POST', body })
}

/** A tender that is still being read answers with what it has read so far and
 * says so in `partial`; the caller polls again while that is set. */
export function getRequirements(tenderId: string): Promise<TenderRequirements> {
  return request<TenderRequirements>(`/api/tenders/${tenderId}/requirements`)
}

/** The same payload, pushed as it changes: `snapshot`, `requirements`, `end`.
 * A browser that cannot read the stream polls the route above instead (D-82). */
export function requirementsStreamUrl(tenderId: string): string {
  return `${BASE}/api/tenders/${tenderId}/requirements/stream`
}

export function getManufacturers(): Promise<Manufacturers> {
  return request<Manufacturers>('/api/manufacturers')
}

/** The documents a tender links to, and the URLs that could not be fetched.
 *
 * A tender is a wrapper: it names its bill of quantities and its
 * specification and embeds neither. When a link has moved, the reading is
 * not wrong — it is incomplete, and the file is still with the buyer. */
export function getAttachments(tenderId: string): Promise<TenderAttachments> {
  return request<TenderAttachments>(`/api/tenders/${tenderId}/attachments`)
}

/** Hand over a document the backend could not fetch.
 *
 * `url` ties the file to the attachment it replaces, so the report keeps
 * saying where the requirement came from. */
export function uploadAttachment(
  tenderId: string,
  file: File,
  url?: string,
): Promise<AttachmentUpload> {
  const body = new FormData()
  body.append('file', file)
  if (url) body.append('url', url)
  // No Content-Type: the browser sets the multipart boundary.
  return request<AttachmentUpload>(`/api/tenders/${tenderId}/attachments`, {
    method: 'POST',
    body,
  })
}

/** Ask the backend to read the same tender again, with the limits in force
 * now. A reading stopped at a budget or rate limit is resumed by this action,
 * not by re-uploading bytes; the new attempt supersedes the old one and the
 * history of earlier attempts is kept. */
export function readAgain(tenderId: string): Promise<{ tender_id: string; status: string }> {
  return request(`/api/tenders/${tenderId}/read-again`, { method: 'POST' })
}

export function startRun(
  tenderId: string,
  manufacturers: string[],
): Promise<RunProgress> {
  return request<RunProgress>('/api/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      tender_id: tenderId,
      // An empty list would mean "none", which the API refuses; omitting the
      // field is how a caller asks for the whole corpus.
      ...(manufacturers.length ? { manufacturers } : {}),
    }),
  })
}

export function getRun(runId: string): Promise<RunProgress> {
  return request<RunProgress>(`/api/runs/${runId}`)
}

export function getReport(runId: string): Promise<Report> {
  return request<Report>(`/api/runs/${runId}/report`)
}

/** The rendered report, for a download link rather than an in-app fetch. */
export function reportUrl(runId: string, kind: 'html' | 'csv'): string {
  return kind === 'html'
    ? `${BASE}/api/runs/${runId}/report.html`
    : `${BASE}/api/runs/${runId}/export.csv`
}

export { BASE as apiBase }
