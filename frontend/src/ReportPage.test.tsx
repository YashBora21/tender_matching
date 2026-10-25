import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'

import { getReport, getRun } from './api'
import { report } from './fixtures'
import { ReportPage } from './components/ReportPage'
import { RunPage } from './components/RunPage'
import type { RunProgress } from './types'

vi.mock('./api', () => ({
  getReport: vi.fn(),
  getRun: vi.fn(),
  reportUrl: (runId: string, kind: string) => `/api/runs/${runId}/${kind}`,
}))

beforeEach(() => {
  vi.mocked(getReport).mockReset()
  vi.mocked(getRun).mockReset()
  vi.mocked(getReport).mockResolvedValue(report())
})

afterEach(cleanup)

function renderReport() {
  return render(<ReportPage runId="run-1" onNewTender={() => {}} />)
}

describe('ReportPage', () => {
  it('renders the ranking and every requirement with its manufacturer', async () => {
    renderReport()

    await screen.findByText('synthetic-tender.pdf')
    expect(screen.getByText('80%')).toBeTruthy()
    expect(screen.getByText('40%')).toBeTruthy()
    expect(screen.getByText('Fails a mandatory requirement')).toBeTruthy()
    // One row per requirement × manufacturer, unsupported clauses included.
    expect(screen.getAllByText('The body shall be stainless steel')).toHaveLength(2)
    expect(screen.getAllByText('Warranty shall be 24 months')).toHaveLength(2)
  })

  it('filters the table from a status count', async () => {
    renderReport()
    await screen.findByText('synthetic-tender.pdf')

    fireEvent.click(screen.getByRole('button', { name: /Missing capability/ }))

    const table = screen.getByRole('table')
    expect(within(table).getByText('Beta')).toBeTruthy()
    expect(within(table).queryByText('Acme')).toBeNull()
    // The status is a filter, not a lost row: clearing it brings Acme back.
    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(within(table).getAllByText('Acme').length).toBeGreaterThan(0)
  })

  it('shows the review queue and jumps from an entry to its row', async () => {
    renderReport()
    await screen.findByText('synthetic-tender.pdf')

    fireEvent.click(screen.getByRole('tab', { name: /Review queue/ }))
    expect(screen.getByText('The corpus says nothing about the end connection')).toBeTruthy()

    fireEvent.click(screen.getAllByRole('button', { name: 'Show in table' })[0])

    // Back on the requirements tab, narrowed to the reviewed manufacturer and
    // status, so the row is visible rather than hunted for.
    const table = screen.getByRole('table')
    expect(within(table).getByText('Beta')).toBeTruthy()
    expect(within(table).queryByText('Acme')).toBeNull()
  })

  it('opens the evidence drawer with the quote, document, page and source link', async () => {
    renderReport()
    await screen.findByText('synthetic-tender.pdf')

    fireEvent.click(screen.getAllByRole('button', { name: 'Evidence' })[0])

    expect(
      screen.getByText('Body: ASTM A351 CF8M (SS316)', { selector: 'blockquote' }),
    ).toBeTruthy()
    expect(screen.getByText('acme-d-series.pdf')).toBeTruthy()
    expect(screen.getByText('12')).toBeTruthy()
    const link = screen.getByRole('link', { name: 'Open the source document' })
    expect(link.getAttribute('href')).toBe('https://manufacturer.test/acme-d-series.pdf')

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('labels an incomplete ingestion instead of hiding it', async () => {
    const partial = report()
    partial.tender.pages = {
      ...partial.tender.pages,
      unaccounted: 2,
      accounted: 2,
      unresolved: [{ page: 3 }],
      incomplete_ingestion: true,
    }
    vi.mocked(getReport).mockResolvedValue(partial)

    renderReport()

    await screen.findByText('Incomplete ingestion')
    expect(screen.getByText(/2 declared page\(s\) were never accounted for/)).toBeTruthy()
    expect(screen.getByText(/Pages that could not be read: 3/)).toBeTruthy()
  })

  it('says when a report is partial instead of presenting it as complete', async () => {
    const partial = report()
    partial.results = { stored: 3, expected: 6, complete: false }
    vi.mocked(getReport).mockResolvedValue(partial)

    renderReport()

    await screen.findByText('This report is partial')
    expect(screen.getByText(/3 of 6 assessments were stored/)).toBeTruthy()
  })
})

describe('RunPage', () => {
  const finished: RunProgress = {
    run_id: 'run-1',
    tender_id: 't1',
    status: 'done',
    manufacturers: ['Acme', 'Beta'],
    requirements: 2,
    expected_results: 4,
    results: 4,
    progress_percent: 100,
    error: null,
  }

  it('moves to the report when the run has stopped', async () => {
    vi.mocked(getRun).mockResolvedValue(finished)

    render(<RunPage runId="run-1" onNewTender={() => {}} />)

    await screen.findByText('synthetic-tender.pdf')
    expect(screen.getByText('Ranking')).toBeTruthy()
  })
})
