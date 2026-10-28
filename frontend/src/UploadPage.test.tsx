import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import {
  getManufacturers,
  getRequirements,
  requirementsStreamUrl,
  startRun,
  uploadTender,
} from './api'
import { UploadPage } from './components/UploadPage'
import type {
  Manufacturers,
  RequirementRow,
  RunProgress,
  TenderRequirements,
  TenderUpload,
} from './types'

vi.mock('./api', () => {
  class ApiError extends Error {
    status: number
    code: string | null = null

    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  }
  return {
    ApiError,
    getManufacturers: vi.fn(),
    getRequirements: vi.fn(),
    requirementsStreamUrl: vi.fn(),
    startRun: vi.fn(),
    uploadTender: vi.fn(),
  }
})

const CORPUS: Manufacturers = {
  count: 2,
  totals: { documents: 20, products: 2, chunks: 200 },
  categories: ['valves', 'pumps'],
  corpus_hash: 'c'.repeat(64),
  manufacturers: [
    {
      id: 'm1',
      name: 'Acme',
      documents: 12,
      products: 1,
      chunks: 120,
      categories: ['valves'],
    },
    {
      id: 'm2',
      name: 'Beta',
      documents: 8,
      products: 1,
      chunks: 80,
      categories: ['pumps'],
    },
  ],
}

function requirementRow(overrides: Partial<RequirementRow> = {}): RequirementRow {
  return {
    id: 'r1',
    item_id: '1',
    text: 'The body shall be stainless steel',
    source_text: null,
    page: 2,
    type: 'material',
    attribute: null,
    review_required: false,
    review_reason: null,
    value: null,
    unit: null,
    mandatory: true,
    item_title: 'Gate valve',
    category: 'valves',
    ...overrides,
  }
}

function read(overrides: Partial<TenderRequirements> = {}): TenderRequirements {
  return {
    tender_id: 't1',
    filename: 'synthetic-tender.pdf',
    status: 'done',
    error: null,
    page_count: 4,
    counts: { items: 1, requirements: 2, flagged: 1 },
    requirements: [
      requirementRow(),
      requirementRow({ id: 'r2', text: 'The end connection shall be flanged' }),
    ],
    ...overrides,
  }
}

/** The browser's event stream, driven by the test instead of a socket. */
class FakeEventSource {
  static last: FakeEventSource | null = null

  readonly url: string
  closed = false
  private listeners = new Map<string, ((event: MessageEvent<string>) => void)[]>()

  constructor(url: string) {
    this.url = url
    FakeEventSource.last = this
  }

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    const known = this.listeners.get(type) ?? []
    known.push(listener)
    this.listeners.set(type, known)
  }

  close() {
    this.closed = true
  }

  emit(type: string, data: unknown) {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ data: JSON.stringify(data) } as MessageEvent<string>)
    }
  }
}

const UPLOAD: TenderUpload = {
  tender_id: 't1',
  status: 'queued',
  duplicate: false,
}

const RUN: RunProgress = {
  run_id: 'run-1',
  tender_id: 't1',
  status: 'retrieving',
  manufacturers: ['Acme', 'Beta'],
  requirements: 2,
  expected_results: 4,
  results: 0,
  progress_percent: 0,
  error: null,
}

beforeEach(() => {
  vi.mocked(getManufacturers).mockReset()
  vi.mocked(getRequirements).mockReset()
  vi.mocked(requirementsStreamUrl).mockReset()
  vi.mocked(startRun).mockReset()
  vi.mocked(uploadTender).mockReset()
  vi.mocked(getManufacturers).mockResolvedValue(CORPUS)
  vi.mocked(uploadTender).mockResolvedValue(UPLOAD)
  vi.mocked(getRequirements).mockResolvedValue(read())
  vi.mocked(startRun).mockResolvedValue(RUN)
  vi.mocked(requirementsStreamUrl).mockImplementation(
    (tenderId) => `http://localhost:8000/api/tenders/${tenderId}/requirements/stream`,
  )
})

afterEach(() => {
  cleanup()
  // The stream is a browser global the test replaces; jsdom has none, so the
  // page falls back to polling unless a test installs this one.
  delete (globalThis as { EventSource?: unknown }).EventSource
  FakeEventSource.last = null
})

function chooseFile(container: HTMLElement, name = 'tender.pdf') {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement
  const file = new File(['%PDF-1.7 synthetic'], name, { type: 'application/pdf' })
  fireEvent.change(input, { target: { files: [file] } })
}

describe('UploadPage', () => {
  it('reads a tender, offers the corpus, and starts the chosen run', async () => {
    const started = vi.fn()
    const { container } = render(<UploadPage onStarted={started} />)

    chooseFile(container)

    await screen.findByText('Choose manufacturers')
    expect(screen.getByText('synthetic-tender.pdf')).toBeTruthy()

    const acme = screen.getByRole('checkbox', { name: /Acme/ }) as HTMLInputElement
    const beta = screen.getByRole('checkbox', { name: /Beta/ }) as HTMLInputElement
    expect(acme.checked).toBe(true)
    expect(beta.checked).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Run the match' }))

    await waitFor(() => expect(started).toHaveBeenCalledWith('run-1'))
    expect(vi.mocked(startRun)).toHaveBeenCalledWith('t1', ['Acme', 'Beta'])
  })

  it('will not ask the server to run when no manufacturer is chosen', async () => {
    const { container } = render(<UploadPage onStarted={() => {}} />)
    chooseFile(container)
    await screen.findByText('Choose manufacturers')

    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))

    const button = screen.getByRole('button', { name: 'Run the match' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    expect(screen.getByText('Choose at least one manufacturer.')).toBeTruthy()
  })

  it('refuses a file that is not a PDF before uploading it', async () => {
    const { container } = render(<UploadPage onStarted={() => {}} />)

    chooseFile(container, 'notes.txt')

    await screen.findByText(/A tender must be a PDF/)
    expect(vi.mocked(uploadTender)).not.toHaveBeenCalled()
  })

  it('shows why a tender could not be read and offers no run', async () => {
    vi.mocked(getRequirements).mockResolvedValue(
      read({
        status: 'failed',
        error: 'no bill of quantities was found: the only attachment is a form',
        requirements: [],
        counts: { items: 0, requirements: 0, flagged: 0 },
      }),
    )
    const { container } = render(<UploadPage onStarted={() => {}} />)

    chooseFile(container)

    await screen.findByText(/no bill of quantities was found/)
    expect(screen.queryByRole('button', { name: 'Run the match' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Upload another tender' })).toBeTruthy()
  })

  it('stops polling at a rate-limited read and offers what was read', async () => {
    vi.mocked(getRequirements).mockResolvedValue(
      read({
        status: 'rate_limited',
        error: 'the model was rate limited; retry in 24.9s',
      }),
    )
    const { container } = render(<UploadPage onStarted={() => {}} />)

    chooseFile(container)

    await screen.findByText(/stopped at the provider's limit/)
    expect(screen.getByText(/retry in 24.9s/)).toBeTruthy()
    // The requirements that were read are listed, and the match is offered.
    await screen.findByText('Choose manufacturers')
    expect(screen.getByRole('button', { name: 'Run the match' })).toBeTruthy()
    // One poll was enough: the state is final until the caller uploads again.
    expect(vi.mocked(getRequirements)).toHaveBeenCalledTimes(1)
  })

  it('fills the list from the stream while the tender is still being read', async () => {
    const globals = globalThis as { EventSource?: unknown }
    globals.EventSource = FakeEventSource
    const started = vi.fn()

    const { container } = render(<UploadPage onStarted={started} />)
    chooseFile(container)

    await waitFor(() => expect(FakeEventSource.last).not.toBeNull())
    const stream = FakeEventSource.last as FakeEventSource
    expect(stream.url).toContain('/api/tenders/t1/requirements/stream')

    // The first requirement is read; the tender is not finished.
    act(() => {
      stream.emit(
        'snapshot',
        read({
          status: 'processing',
          partial: true,
          counts: { items: 1, requirements: 1, flagged: 0 },
          requirements: [requirementRow()],
        }),
      )
    })

    await screen.findByText('Requirements read so far')
    expect(screen.getByText(/1 requirement read so far/)).toBeTruthy()
    expect(screen.getByText('The body shall be stainless steel')).toBeTruthy()
    // A partial read is not a finished one: no manufacturers are offered yet.
    expect(screen.queryByText('Choose manufacturers')).toBeNull()

    // The second one arrives, and the list grows without a page reload.
    act(() => {
      stream.emit(
        'requirements',
        read({
          status: 'processing',
          partial: true,
          counts: { items: 1, requirements: 2, flagged: 1 },
          requirements: [
            requirementRow(),
            requirementRow({ id: 'r2', text: 'The end connection shall be flanged' }),
          ],
        }),
      )
    })
    await screen.findByText('The end connection shall be flanged')
    expect(screen.getByText(/2 requirements read so far/)).toBeTruthy()

    // The read stops, so the page moves on and closes the connection.
    act(() => {
      stream.emit('end', read())
    })
    await screen.findByText('Choose manufacturers')
    expect(stream.closed).toBe(true)
    // The stream carried every change, so the poll never ran.
    expect(vi.mocked(getRequirements)).not.toHaveBeenCalled()
  })

  it('falls back to polling when the stream breaks before the read finishes', async () => {
    const globals = globalThis as { EventSource?: unknown }
    globals.EventSource = FakeEventSource
    vi.mocked(getRequirements).mockResolvedValue(read())

    const { container } = render(<UploadPage onStarted={() => {}} />)
    chooseFile(container)

    await waitFor(() => expect(FakeEventSource.last).not.toBeNull())
    const stream = FakeEventSource.last as FakeEventSource

    act(() => {
      stream.emit('error', {})
    })

    // The plain route answers, so the page still shows the tender.
    await screen.findByText('Choose manufacturers')
    expect(stream.closed).toBe(true)
    expect(vi.mocked(getRequirements)).toHaveBeenCalled()
  })
})
