/** The backend client (PRD 11).
 *
 * The API is the only thing that decides: this module sends what the caller
 * typed and returns what the answer says. Nothing here invents a status, a
 * score or an evidence quote.
 */

import type {
  Manufacturers,
  Report,
  RunProgress,
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

/** A tender that is still being read answers 409; the caller polls again. */
export function getRequirements(tenderId: string): Promise<TenderRequirements> {
  return request<TenderRequirements>(`/api/tenders/${tenderId}/requirements`)
}

export function getManufacturers(): Promise<Manufacturers> {
  return request<Manufacturers>('/api/manufacturers')
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
