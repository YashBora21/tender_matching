import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import { getManufacturers, getRequirements, startRun, uploadTender } from './api'
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
  vi.mocked(startRun).mockReset()
  vi.mocked(uploadTender).mockReset()
  vi.mocked(getManufacturers).mockResolvedValue(CORPUS)
  vi.mocked(uploadTender).mockResolvedValue(UPLOAD)
  vi.mocked(getRequirements).mockResolvedValue(read())
  vi.mocked(startRun).mockResolvedValue(RUN)
})

afterEach(cleanup)

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
})
