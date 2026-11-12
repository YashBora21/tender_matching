import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import {
  ApiError,
  getAttachments,
  getManufacturers,
  getRequirements,
  readAgain,
  requirementsStreamUrl,
  startRun,
  uploadAttachment,
  uploadTender,
} from './api'
import { UploadPage } from './components/UploadPage'
import type {
  AttachmentRow,
  AttachmentUpload,
  Manufacturers,
  RequirementRow,
  RunProgress,
  TenderAttachments,
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
    getAttachments: vi.fn(),
    getManufacturers: vi.fn(),
    getRequirements: vi.fn(),
    readAgain: vi.fn(),
    requirementsStreamUrl: vi.fn(),
    startRun: vi.fn(),
    uploadAttachment: vi.fn(),
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

const NO_ATTACHMENTS: TenderAttachments = {
  tender_id: 't1',
  category: 'pumps',
  attachments: [],
  missing: [],
}

function attachment(overrides: Partial<AttachmentRow> = {}): AttachmentRow {
  return {
    url: 'https://buyer.example/spec-1.pdf',
    name: 'spec-1.pdf',
    sha256: 'a'.repeat(64),
    kind: 'spec',
    status: 'ok',
    bytes: 20480,
    pages: 12,
    retrieved_at: '2026-10-21T09:00:00+00:00',
    note: null,
    ...overrides,
  }
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
  llm_calls: 0,
  prompt_tokens: 0,
  completion_tokens: 0,
  llm_retries: 0,
  stage_seconds: {},
  error: null,
}

beforeEach(() => {
  vi.mocked(getAttachments).mockReset()
  vi.mocked(uploadAttachment).mockReset()
  vi.mocked(getManufacturers).mockReset()
  vi.mocked(getRequirements).mockReset()
  vi.mocked(readAgain).mockReset()
  vi.mocked(requirementsStreamUrl).mockReset()
  vi.mocked(startRun).mockReset()
  vi.mocked(uploadTender).mockReset()
  vi.mocked(getManufacturers).mockResolvedValue(CORPUS)
  vi.mocked(getAttachments).mockResolvedValue(NO_ATTACHMENTS)
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
  it('shows stored source context separately and permits rereading a complete tender', async () => {
    vi.mocked(getRequirements).mockResolvedValue(read({
      context_clauses: [requirementRow({ id: 'context-1', type: 'commercial',
        source_role: 'cover', text: 'Synthetic delivery within 30 days' })],
    }))
    const { container } = render(<UploadPage onStarted={() => {}} />)
    chooseFile(container)
    expect(await screen.findByText('Source context (1)')).toBeTruthy()
    expect(screen.getByText('Synthetic delivery within 30 days')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Read this tender again' })).toBeTruthy()
  })

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

  it('groups the corpus by category and offers only the one this tender asks about', async () => {
    const { container } = render(<UploadPage onStarted={() => {}} />)

    chooseFile(container)

    await screen.findByText('Choose manufacturers')
    // The tender's requirements sit under items whose category is `valves`
    // (FR-16), so the valve manufacturers are their own group, listed first.
    expect(screen.getByRole('heading', { name: /Valves/ })).toBeTruthy()
    expect(screen.getByRole('heading', { name: /Pumps/ })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Only valves (1)' }))

    const acme = screen.getByRole('checkbox', { name: /Acme/ }) as HTMLInputElement
    const beta = screen.getByRole('checkbox', { name: /Beta/ }) as HTMLInputElement
    expect(acme.checked).toBe(true)
    expect(beta.checked).toBe(false)
  })

  it('leaves the corpus flat when the tender names no category', async () => {
    vi.mocked(getRequirements).mockResolvedValue(
      read({ requirements: [requirementRow({ category: null })] }),
    )
    const { container } = render(<UploadPage onStarted={() => {}} />)

    chooseFile(container)

    await screen.findByText('Choose manufacturers')
    // No category, no narrowing: an item that names none is not guessed at.
    expect(screen.queryByRole('button', { name: /^Only / })).toBeNull()
    expect(screen.queryByRole('heading', { name: /Valves/ })).toBeNull()
    expect(screen.getByRole('checkbox', { name: /Beta/ })).toBeTruthy()
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

    await screen.findByText(/reached a processing limit/)
    expect(screen.getByText(/retry in 24.9s/)).toBeTruthy()
    await screen.findByText('Saved requirements')
    expect(screen.queryByRole('button', { name: 'Run the match' })).toBeNull()
    // One poll was enough: the state is final until the caller uploads again.
    expect(vi.mocked(getRequirements)).toHaveBeenCalledTimes(1)
  })

  it('resumes a stopped reading by asking for it again, not by re-uploading', async () => {
    const globals = globalThis as { EventSource?: unknown }
    globals.EventSource = FakeEventSource
    vi.mocked(getRequirements).mockResolvedValue(
      read({ status: 'rate_limited', error: 'the call budget stopped the read' }),
    )
    vi.mocked(readAgain).mockResolvedValue({ tender_id: 't1', status: 'queued' })

    const { container } = render(<UploadPage onStarted={() => {}} />)
    chooseFile(container)

    await waitFor(() => expect(FakeEventSource.last).not.toBeNull())
    // The first attempt stops at the limit, so the page offers the resume.
    const first = FakeEventSource.last as FakeEventSource
    act(() => {
      first.emit(
        'end',
        read({ status: 'rate_limited', error: 'the call budget stopped the read' }),
      )
    })
    await screen.findByRole('button', { name: 'Read this tender again' })

    fireEvent.click(screen.getByRole('button', { name: 'Read this tender again' }))

    // The resume action went to the read-again route with the tender's id.
    await waitFor(() => expect(vi.mocked(readAgain)).toHaveBeenCalledWith('t1'))
    // The initial upload is the only one: the resume asked for the read, not
    // for the bytes a second time.
    expect(vi.mocked(uploadTender)).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(FakeEventSource.last).not.toBe(first))
    expect(FakeEventSource.last?.url).toContain('/api/tenders/t1/requirements/stream')
  })

  it('does not call an empty budget stop a tender with no requirements', async () => {
    vi.mocked(getRequirements).mockResolvedValue(read({
      status: 'rate_limited', requirements: [],
      counts: { items: 1, requirements: 0, flagged: 0 }, error: 'Synthetic call budget exhausted',
    }))
    const { container } = render(<UploadPage onStarted={() => {}} />)
    chooseFile(container)
    await screen.findByText('No requirements have been published yet')
    expect(screen.queryByText(/The tender was read; the documents it contains state no technical requirement/)).toBeNull()
  })

  it('names an incomplete read, and refuses to run it', async () => {
    vi.mocked(getRequirements).mockResolvedValue(
      read({
        status: 'incomplete',
        error: 'the bill of quantities gave no items',
        requirements: [],
        counts: { items: 0, requirements: 0, flagged: 0 },
      }),
    )
    const { container } = render(<UploadPage onStarted={() => {}} />)

    chooseFile(container)

    // Neither "still reading" nor "failed": the document opened and came up short.
    await screen.findByText('The tender was not read in full')
    expect(screen.getByText(/the bill of quantities gave no items/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Run the match' })).toBeNull()
    // One poll was enough: the state is final until the caller uploads again.
    expect(vi.mocked(getRequirements)).toHaveBeenCalledTimes(1)
  })

  it('names which way an incomplete read fell short', async () => {
    vi.mocked(getRequirements).mockResolvedValue(
      read({
        status: 'incomplete',
        reason_code: 'specification_unreadable',
        error: 'the specification is a scan and the OCR read no text from it',
        requirements: [],
        counts: { items: 0, requirements: 0, flagged: 0 },
      }),
    )
    const { container } = render(<UploadPage onStarted={() => {}} />)

    chooseFile(container)

    // A spec nobody could read is not a spec that states nothing, and the
    // screen has to say which of the three it is (D-156).
    await screen.findByText('The specification is a scan nobody could read')
    expect(screen.getByText(/OCR gave little or no text/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Run the match' })).toBeNull()
  })

  it('says when the document was read and states nothing', async () => {
    vi.mocked(getRequirements).mockResolvedValue(
      read({
        status: 'incomplete',
        reason_code: 'no_requirements_stated',
        error: 'the specification gave no requirements',
        requirements: [],
        counts: { items: 1, requirements: 0, flagged: 0 },
      }),
    )
    const { container } = render(<UploadPage onStarted={() => {}} />)

    chooseFile(container)

    await screen.findByText('The specification was read and states no requirement')
    expect(
      screen.queryByText(/Supply the missing document above/),
    ).toBeNull()
  })

  it('does not prescribe a missing file for interpretation failures', async () => {
    vi.mocked(getRequirements).mockResolvedValue(read({
      status: 'incomplete', reason_code: null,
      error: 'Technical clauses have no extracted attributes',
    }))
    const { container } = render(<UploadPage onStarted={() => {}} />)
    chooseFile(container)
    await screen.findByText('Technical clauses have no extracted attributes')
    expect(screen.getByText(/Review unresolved clauses, BOQ item assignments/)).toBeTruthy()
    expect(screen.queryByText(/Supply the missing document above/)).toBeNull()
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
    expect(screen.getByText(/1 requirement in the last saved reading attempt/)).toBeTruthy()
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
    expect(screen.getByText(/2 requirements in the last saved reading attempt/)).toBeTruthy()

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

  it('offers a document the fetch could not reach and takes the reviewer copy of it', async () => {
    vi.mocked(getAttachments).mockResolvedValue({
      ...NO_ATTACHMENTS,
      attachments: [
        attachment(),
        attachment({
          url: 'https://buyer.example/boq-2.csv',
          name: 'boq-2.csv',
          kind: 'boq-csv',
          status: 'failed',
          sha256: null,
          bytes: null,
          pages: null,
          note: 'the host refused the connection',
        }),
        attachment({
          url: 'https://buyer.example/form-3.pdf',
          name: 'form-3.pdf',
          kind: 'form',
          status: 'restricted',
          sha256: null,
          note: 'the host robots.txt disallows this path',
        }),
      ],
      missing: ['https://buyer.example/boq-2.csv', 'https://buyer.example/form-3.pdf'],
    })
    vi.mocked(uploadAttachment).mockResolvedValue({
      tender_id: 't1',
      name: 'boq-2.csv',
      url: 'https://buyer.example/boq-2.csv',
      sha256: 'f'.repeat(64),
      kind: 'boq-csv',
      bytes: 512,
      pages: null,
      status: 'manual',
    } satisfies AttachmentUpload)

    const { container } = render(<UploadPage onStarted={() => {}} />)
    chooseFile(container)

    await screen.findByText('Attachments')
    expect(await screen.findByText('spec-1.pdf')).toBeTruthy()
    expect(screen.getByText(/the host refused the connection/)).toBeTruthy()
    // Only what was not fetched is offered for supply; the fetched document
    // is listed for provenance and left alone.
    expect(await screen.findAllByText('Supply this document')).toHaveLength(2)

    const file = new File(['1,2,3 synthetic'], 'boq-2.csv', { type: 'text/csv' })
    const supplied = container.querySelectorAll('input[type="file"]')[1]
    fireEvent.change(supplied, { target: { files: [file] } })

    await waitFor(() => expect(uploadAttachment).toHaveBeenCalled())
    expect(vi.mocked(uploadAttachment)).toHaveBeenCalledWith(
      't1',
      file,
      'https://buyer.example/boq-2.csv',
    )
    // The list is asked again, so the row now reads what the record says.
    await waitFor(() => expect(vi.mocked(getAttachments)).toHaveBeenCalledTimes(2))
    expect(screen.getByText(/is stored against this tender/)).toBeTruthy()
  })

  it('says there is nothing to supply when the tender records no document', async () => {
    const { container } = render(<UploadPage onStarted={() => {}} />)
    chooseFile(container)

    await screen.findByText('Attachments')
    expect(
      await screen.findByText(/No document is recorded for this tender/),
    ).toBeTruthy()
    expect(container.querySelectorAll('input[type="file"]')).toHaveLength(1)
  })

  it('still offers the match when the attachments cannot be listed', async () => {
    vi.mocked(getAttachments).mockRejectedValue(
      new ApiError(500, 'the manifest could not be read'),
    )
    const started = vi.fn()
    const { container } = render(<UploadPage onStarted={started} />)
    chooseFile(container)

    await screen.findByText('Choose manufacturers')
    await screen.findByText('the manifest could not be read')
    fireEvent.click(screen.getByRole('button', { name: 'Run the match' }))
    await waitFor(() => expect(started).toHaveBeenCalledWith('run-1'))
  })
})
