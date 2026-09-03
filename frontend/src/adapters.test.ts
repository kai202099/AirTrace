import { describe, expect, it } from 'vitest'
import { formatAge, formatReplayWindow, formatTaipei, incidentFromDetail } from './adapters'

describe('data adapters', () => {
  it('converts incident artifact fields without inventing a probability', () => {
    const summary = incidentFromDetail({ incident: { incident_id: 'i-1', event_id: 'e-1', analysis_zone: 'core', event: { start_time_utc: '2026-09-02T20:30:00Z', last_seen_time_utc: '2026-09-02T20:33:00Z', duration_minutes: 3, unique_sensor_count: 4, peak_pm25: 31.2, max_anomaly_score: 7, event_strength: 0.64 }, wind: {}, trace: { status: 'TRACE_COMPLETE' }, evidence: { status: 'EVIDENCE_COMPLETE', firms_status: 'NO FIRMS HOTSPOT DETECTED', cems_status: 'CEMS_CONTEXT_UNAVAILABLE' }, limitations: ['uncertain'] }, membership: [], source_evidence: [], facilities: [], fires: [] })
    expect(summary.sensor_count).toBe(4)
    expect(summary.event_strength).toBe(0.64)
    expect(summary.firms_status).toContain('NO FIRMS')
  })

  it('formats Taipei time and bounded freshness', () => {
    expect(formatTaipei('2026-09-02T20:30:00Z')).toContain('04:30')
    expect(formatAge(12)).toBe('12 min ago')
    expect(formatAge(null)).toBe('No observation')
  })

  it('formats a replay window separately from live freshness', () => {
    expect(formatReplayWindow('2026-09-02T16:45:00Z', '2026-09-02T20:30:00Z')).toContain('03 Sep 00:45–04:30')
  })
})
