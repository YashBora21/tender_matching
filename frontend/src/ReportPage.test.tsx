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
  it('searches catalogue capabilities and restores rows when filters are cleared', async () => {
    renderReport()
    await screen.findByText('synthetic-tender.pdf')
    expect(screen.getByRole('columnheader', { name: 'Catalogue capability' })).toBeTruthy()
    const search = screen.getByRole('searchbox', { name: 'Search requirements' })
    fireEvent.change(search, { target: { value: 'CF8M' } })
    expect(within(screen.getByRole('table')).queryByText('Warranty shall be 24 months')).toBeNull()
    expect(within(screen.getByRole('table')).getAllByText('Body: ASTM A351 CF8M (SS316)').length).toBeGreaterThan(0)
    fireEvent.change(search, { target: { value: 'synthetic absent value' } })
    expect(screen.getByText('No rows match these filters.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(screen.getByRole('table')).toBeTruthy()
    expect((search as HTMLInputElement).value).toBe('')
  })
  it('keeps fragments in a collapsed source view without adding assessments', async () => {
    vi.mocked(getReport).mockResolvedValue(report({context_clauses: [{
      requirement_id: 'fragment-1', text: 'Loose <script>scrap</script>',
      source_text: 'Loose <script>scrap</script>', page: 2, type: 'unknown',
      source_role: 'cover', fragment_reason: 'fragment (below prose floor, no signal)',
    }]}))
    const { container } = renderReport()
    const summary = await screen.findByText('Fragments (1)')
    expect(summary.closest('details')?.hasAttribute('open')).toBe(false)
    expect(screen.getAllByText('Loose <script>scrap</script>')).toHaveLength(1)
    expect(container.querySelector('script')).toBeNull()
    expect(screen.getAllByText('The body shall be stainless steel')).toHaveLength(2)
  })

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

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search requirements' }), { target: { value: 'synthetic absent value' } })

    fireEvent.click(screen.getByRole('tab', { name: /Review queue/ }))
    expect(screen.getByText('The corpus says nothing about the end connection')).toBeTruthy()
    // The queue points at what to look at; it is not a place to record a decision.
    expect(screen.getByText(/does not record the decision/)).toBeTruthy()

    fireEvent.click(screen.getAllByRole('button', { name: 'Show in table' })[0])
    expect((screen.getByRole('searchbox') as HTMLInputElement).value).toBe('')

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
    expect(screen.getByText('Ranking unavailable')).toBeTruthy()
    expect(screen.queryByText('80%')).toBeNull()
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
    llm_calls: 0,
    prompt_tokens: 0,
    completion_tokens: 0,
    llm_retries: 0,
    stage_seconds: {},
    error: null,
  }

  it('shows where a working run has spent its time, as it reports it', async () => {
    vi.mocked(getRun).mockResolvedValue({
      ...finished,
      status: 'scoring',
      results: 1,
      progress_percent: 25,
      llm_calls: 3,
      prompt_tokens: 4200,
      completion_tokens: 180,
      llm_retries: 1,
      stage_seconds: { retrieve: 0.412, extract: 26.889, store: 0.02 },
    })

    render(<RunPage runId="run-1" onNewTender={() => {}} />)

    await screen.findByText('1 of 4 assessments')
    expect(screen.getByText('26.889 s')).toBeTruthy()
    expect(screen.getByText('extract')).toBeTruthy()
    expect(screen.getByText('4200')).toBeTruthy()
    expect(screen.getByText('Retries')).toBeTruthy()
  })

  it('says nothing is measured yet instead of a column of zeros', async () => {
    vi.mocked(getRun).mockResolvedValue({
      ...finished,
      status: 'retrieving',
      results: 0,
      progress_percent: 0,
    })

    render(<RunPage runId="run-1" onNewTender={() => {}} />)

    await screen.findByText('Nothing yet: the first pair is still being read.')
    expect(screen.queryByText('0.000 s')).toBeNull()
    expect(screen.queryByText('Model calls')).toBeNull()
  })

  it('moves to the report when the run has stopped', async () => {
    vi.mocked(getRun).mockResolvedValue(finished)

    render(<RunPage runId="run-1" onNewTender={() => {}} />)

    await screen.findByText('synthetic-tender.pdf')
    expect(screen.getByText('Ranking')).toBeTruthy()
  })

  it('serves the report of a run the provider stopped', async () => {
    // The API treats `rate_limited` as terminal and answers with the report of
    // what it stored. A page that polled on would never show those answers.
    vi.mocked(getRun).mockResolvedValue({
      ...finished,
      status: 'rate_limited',
      results: 2,
      progress_percent: 50,
      error: 'the model was rate limited',
    })

    render(<RunPage runId="run-1" onNewTender={() => {}} />)

    await screen.findByText('synthetic-tender.pdf')
    expect(screen.getByText('Ranking')).toBeTruthy()
  })
})
