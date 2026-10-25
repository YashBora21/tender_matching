import { describe, expect, it } from 'vitest'

import { report, requirement, result } from './fixtures'
import {
  assessments,
  countStatuses,
  filterAssessments,
  formatPercent,
  formatScore,
  label,
  requirementText,
  statusesPresent,
} from './report'

describe('assessments', () => {
  it('flattens supported and unsupported requirements into one row per result', () => {
    const data = report({
      requirements: [
        requirement({
          results: [result(), result({ manufacturer_id: 'm2', manufacturer: 'Beta' })],
        }),
      ],
      unsupported_requirements: [
        requirement({
          requirement_id: 'r2',
          text: 'Price shall be valid for 90 days',
          results: [result({ status: 'unsupported_requirement' })],
        }),
      ],
    })

    const rows = assessments(data)

    expect(rows).toHaveLength(3)
    expect(rows.filter((row) => row.unsupported)).toHaveLength(1)
    // The unsupported clause is kept, not hidden: it is still a row a reviewer
    // asks about, even though it is excluded from the score.
    expect(rows[2].requirement.requirement_id).toBe('r2')
  })
})

describe('filterAssessments', () => {
  const rows = assessments(
    report({
      requirements: [
        requirement({
          results: [
            result({ manufacturer: 'Acme', status: 'full_match' }),
            result({
              manufacturer: 'Beta',
              status: 'no_evidence_found',
              score: null,
              review_required: true,
              review_reason: 'the corpus is silent',
            }),
          ],
        }),
      ],
      unsupported_requirements: [],
    }),
  )

  it('keeps every row when nothing is filtered', () => {
    expect(filterAssessments(rows, { status: null, manufacturer: null, reviewOnly: false })).toHaveLength(2)
  })

  it('filters by status', () => {
    const kept = filterAssessments(rows, {
      status: 'no_evidence_found',
      manufacturer: null,
      reviewOnly: false,
    })
    expect(kept.map((row) => row.result.manufacturer)).toEqual(['Beta'])
  })

  it('filters by manufacturer', () => {
    const kept = filterAssessments(rows, {
      status: null,
      manufacturer: 'Acme',
      reviewOnly: false,
    })
    expect(kept).toHaveLength(1)
    expect(kept[0].result.status).toBe('full_match')
  })

  it('filters to rows that need review, and combines with the other filters', () => {
    expect(
      filterAssessments(rows, { status: null, manufacturer: null, reviewOnly: true }),
    ).toHaveLength(1)
    expect(
      filterAssessments(rows, {
        status: 'full_match',
        manufacturer: null,
        reviewOnly: true,
      }),
    ).toHaveLength(0)
  })
})

describe('counts and labels', () => {
  it('counts every assessment, including unsupported ones', () => {
    const rows = assessments(
      report({
        requirements: [requirement({ results: [result(), result({ status: 'partial_match' })] })],
        unsupported_requirements: [
          requirement({
            requirement_id: 'r2',
            results: [result({ status: 'unsupported_requirement' })],
          }),
        ],
      }),
    )

    expect(countStatuses(rows)).toEqual({
      full_match: 1,
      partial_match: 1,
      unsupported_requirement: 1,
    })
  })

  it('lists the statuses present in the PRD order', () => {
    expect(
      statusesPresent({
        no_evidence_found: 2,
        full_match: 1,
        unsupported_requirement: 3,
        not_a_status: 1,
      }),
    ).toEqual(['full_match', 'unsupported_requirement', 'no_evidence_found', 'not_a_status'])
  })

  it('leaves an unknown word readable without inventing a meaning', () => {
    expect(label('full_match')).toBe('Full match')
    expect(label('some_new_state')).toBe('some new state')
    expect(label(null)).toBe('—')
  })

  it('shows no score as absent, not as zero', () => {
    expect(formatScore(null)).toBe('—')
    expect(formatScore(0)).toBe('0%')
    expect(formatScore(0.755)).toBe('76%')
    expect(formatPercent(null)).toBe('—')
    expect(formatPercent(72.25)).toBe('72.3%')
  })

  it('finds the text of a reviewed requirement in either list', () => {
    const data = report({
      unsupported_requirements: [
        requirement({ requirement_id: 'r9', text: 'Warranty shall be 24 months' }),
      ],
    })

    expect(
      requirementText(data, {
        requirement_id: 'r9',
        manufacturer: 'Acme',
        status: 'unsupported_requirement',
        reason: null,
      }),
    ).toBe('Warranty shall be 24 months')
    expect(
      requirementText(data, {
        requirement_id: 'missing',
        manufacturer: 'Acme',
        status: 'full_match',
        reason: null,
      }),
    ).toBeNull()
  })
})
