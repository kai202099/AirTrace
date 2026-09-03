import { describe, expect, it } from 'vitest'
import { eventSensorRows, facilityMapRows, formatAge, formatReplayWindow, formatTaipei, incidentFromDetail, isSynthetic, OSM_ATTRIBUTION, OSM_RASTER_PAINT, sourceEvidenceFeatures, windArrowFeatures } from './adapters'

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

  it('uses explicit synthetic provenance from the API instead of a directory name', () => {
    expect(isSynthetic({ run_id: 'renamed-demo', synthetic_validation: true } as any)).toBe(true)
    expect(isSynthetic({ run_id: 'synthetic-looking-name', synthetic_validation: false } as any)).toBe(false)
  })

  it('uses event membership for anomaly sensors and can render artifact-only members', () => {
    const rows = eventSensorRows([
      { station_id: 'event-a', station_name: 'Event A', lat: 25.06, lon: 121.45, pm25: 30, timestamp_utc: '2026-09-02T20:30:00Z', freshness: 'historical', age_minutes: 0 },
      { station_id: 'context-only', station_name: 'Context', lat: 25.07, lon: 121.46, pm25: 30, timestamp_utc: null, freshness: 'historical', age_minutes: null },
    ], [
      { station_id: 'event-a', lat: '25.06', lon: '121.45', role: 'seed' },
      { station_id: 'event-b', lat: '25.0602', lon: '121.4502', role: 'seed', pm25: '30', time_bin: '2026-09-02T20:30:00Z' },
    ])
    expect(rows.map((row) => row.station_id)).toEqual(['event-a', 'event-b'])
    expect(rows.find((row) => row.station_id === 'event-b')?.freshness).toBe('historical')
  })

  it('builds wind arrows from existing vectors in the TO direction', () => {
    const [feature] = windArrowFeatures([{ lat: 25.06, lon: 121.45, u_east_mps: 1, v_north_mps: 0, time_utc: '2026-09-02T20:29:00Z' }])
    expect(feature.properties.source).toBe('wind_diagnostics.map_arrows')
    expect(feature.properties.wind_to_deg).toBe(90)
    expect(feature.geometry.coordinates[0][0]).toEqual([121.45, 25.06])
    expect(feature.geometry.coordinates[0][1][0]).toBeGreaterThan(121.45)
  })

  it('degrades gracefully when wind artifacts are missing', () => {
    expect(windArrowFeatures(undefined)).toEqual([])
    expect(windArrowFeatures([{ lat: 25.06, lon: 121.45, u_east_mps: 0, v_north_mps: 2 }])).toHaveLength(1)
    expect(windArrowFeatures([{ lat: 25.06, lon: 121.45, u_east_mps: 0, v_north_mps: 0 }])).toEqual([])
  })

  it('uses aggregate source evidence and ignores raw trajectory count', () => {
    const trace = {
      display_trajectories: Array.from({ length: 120 }, () => ({ points: [[121, 25]] })),
      source_evidence_grid: [{ center_lat: 25.06, center_lon: 121.45, source_evidence_score: 0.8 }],
      candidate_source_regions: [{ rank: 1 }],
    }
    const features = sourceEvidenceFeatures(trace)
    expect(features).toHaveLength(1)
    expect(features[0].properties.score).toBe(0.8)
    expect(JSON.stringify(features)).not.toContain('display_trajectories')
  })

  it('keeps facility ranking and evidence fields while limiting map matches', () => {
    const rows = [1, 3, 2, 4, 5, 6].map((rank) => ({ rank: String(rank), lat: '25.06', lon: String(121.4 + rank / 100), evidence_score: String(1 - rank / 10), distance_to_top_source_region_km: '2.5' }))
    const features = facilityMapRows(rows, 5)
    expect(features.map((row) => row.rank)).toEqual(['1', '2', '3', '4', '5'])
    expect(features.map((row) => row.map_label)).toEqual(['F1', 'F2', 'F3', 'F4', 'F5'])
    expect(features[0].evidence_score).toBe('0.9')
    expect(features[0].distance_to_top_source_region_km).toBe('2.5')
  })

  it('keeps OSM attribution while applying a subdued raster treatment', () => {
    expect(OSM_ATTRIBUTION).toContain('openstreetmap.org/copyright')
    expect(OSM_RASTER_PAINT['raster-opacity']).toBeLessThan(1)
    expect(OSM_RASTER_PAINT['raster-saturation']).toBeLessThan(0)
  })
})
