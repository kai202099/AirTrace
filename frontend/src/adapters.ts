import type { IncidentDetail, IncidentSummary, RunDetail, RunIndex, Sensor } from './types'

const asNumber = (value: unknown) => {
  const number = Number(value)
  return Number.isFinite(number) ? number : 0
}

const finiteNumber = (value: unknown): number | null => {
  if (value == null || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

export const eventSensorRows = (sensors: readonly Sensor[], membership: readonly Record<string, unknown>[]): Sensor[] => {
  const sensorsByStation = new Map(sensors.map((sensor) => [sensor.station_id, sensor]))
  const seen = new Set<string>()
  const result: Sensor[] = []
  for (const row of membership) {
    const stationId = String(row.station_id ?? '').trim()
    if (!stationId || seen.has(stationId)) continue
    seen.add(stationId)
    const sensor = sensorsByStation.get(stationId)
    const lat = finiteNumber(sensor?.lat) ?? finiteNumber(row.lat)
    const lon = finiteNumber(sensor?.lon) ?? finiteNumber(row.lon)
    if (lat == null || lon == null) continue
    result.push({
      station_id: stationId,
      station_name: sensor?.station_name ?? (row.station_name == null ? undefined : String(row.station_name)),
      lat,
      lon,
      pm25: sensor?.pm25 ?? finiteNumber(row.pm25),
      timestamp_utc: sensor?.timestamp_utc ?? (row.time_bin == null ? null : String(row.time_bin)),
      freshness: sensor?.freshness ?? 'historical',
      age_minutes: sensor?.age_minutes ?? null,
      source_status: sensor?.source_status ?? (row.source_status == null ? undefined : String(row.source_status)),
      quality_flags: sensor?.quality_flags ?? (row.quality_flags == null ? undefined : String(row.quality_flags)),
    })
  }
  return result
}

export type GeoFeature = {
  type: 'Feature'
  geometry: { type: string; coordinates: unknown }
  properties: Record<string, unknown>
}

type WindArrowFeature = {
  type: 'Feature'
  geometry: { type: 'MultiLineString'; coordinates: [number, number][][] }
  properties: Record<string, unknown>
}

export const OSM_ATTRIBUTION = '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">© OpenStreetMap contributors</a>'
export const OSM_RASTER_PAINT = {
  'raster-saturation': -0.75,
  'raster-contrast': -0.3,
  'raster-opacity': 0.42,
  'raster-brightness-min': 0.22,
  'raster-brightness-max': 0.86,
}

const SOURCE_GRID_CELL_METERS = 250
const NORMALIZED_WIND_ARROW_LENGTH_METERS = 2_000

const recordValue = (value: unknown): Record<string, unknown> | null => value && typeof value === 'object' ? value as Record<string, unknown> : null

export const sourceEvidenceFeatures = (trace: Record<string, unknown> | null | undefined): GeoFeature[] => {
  const grid = Array.isArray(trace?.source_evidence_grid) ? trace.source_evidence_grid : []
  const traceConfig = recordValue(trace?.trace_config)
  const configuredCellMeters = finiteNumber(traceConfig?.grid_cell_m)
  const cellMeters = configuredCellMeters != null && configuredCellMeters > 0 ? configuredCellMeters : SOURCE_GRID_CELL_METERS
  const latitudeMeters = 111_320
  return grid.flatMap((value): GeoFeature[] => {
    const cell = recordValue(value)
    if (!cell) return []
    const lat = finiteNumber(cell.center_lat)
    const lon = finiteNumber(cell.center_lon)
    if (lat == null || lon == null) return []
    const score = finiteNumber(cell.source_evidence_score) ?? finiteNumber(cell.normalized_density) ?? 0
    const halfLat = cellMeters / (2 * latitudeMeters)
    const halfLon = halfLat / Math.max(0.01, Math.cos((lat * Math.PI) / 180))
    return [{
      type: 'Feature',
      geometry: { type: 'Polygon', coordinates: [[
        [lon - halfLon, lat - halfLat], [lon + halfLon, lat - halfLat],
        [lon + halfLon, lat + halfLat], [lon - halfLon, lat + halfLat],
        [lon - halfLon, lat - halfLat],
      ]] },
      properties: {
        label: 'Relative source evidence',
        score: Math.max(0, Math.min(1, score)),
        normalized_density: finiteNumber(cell.normalized_density),
        receptor_support_count: finiteNumber(cell.receptor_support_count),
        receptor_support_fraction: finiteNumber(cell.receptor_support_fraction),
      },
    }]
  })
}

export const facilityMapRows = (facilities: readonly Record<string, unknown>[], maximum = 5): Record<string, unknown>[] => {
  const ranked = facilities.map((row, index) => ({ row, index, rank: finiteNumber(row.rank) }))
    .filter(({ row }) => finiteNumber(row.lat) != null && finiteNumber(row.lon) != null)
    .sort((left, right) => {
      if (left.rank == null && right.rank == null) return left.index - right.index
      if (left.rank == null) return 1
      if (right.rank == null) return -1
      return left.rank - right.rank || left.index - right.index
    })
    .slice(0, Math.max(0, maximum))
  return ranked.map(({ row, rank }, displayedIndex) => {
    const displayRank = rank ?? displayedIndex + 1
    return { ...row, map_rank: displayRank, map_label: `F${displayRank}`, map_semantics: 'Candidate evidence only; not a detected source' }
  })
}

export const windArrowFeatures = (
  arrows: unknown,
  maximum = 8,
): WindArrowFeature[] => {
  const candidates = (Array.isArray(arrows) ? arrows : []).filter((arrow): arrow is Record<string, unknown> => {
    if (!arrow || typeof arrow !== 'object') return false
    return finiteNumber(arrow.lat) != null && finiteNumber(arrow.lon) != null && finiteNumber(arrow.u_east_mps) != null && finiteNumber(arrow.v_north_mps) != null
  })
  return candidates.slice(0, maximum).flatMap((arrow) => {
    const lat = finiteNumber(arrow.lat)
    const lon = finiteNumber(arrow.lon)
    const u = finiteNumber(arrow.u_east_mps)
    const v = finiteNumber(arrow.v_north_mps)
    if (lat == null || lon == null || u == null || v == null) return []
    const speed = Math.hypot(u, v)
    if (speed <= 0) return []
    const latitudeScale = 111_320
    const longitudeScale = latitudeScale * Math.max(0.01, Math.cos((lat * Math.PI) / 180))
    const dx = (u / speed * NORMALIZED_WIND_ARROW_LENGTH_METERS) / longitudeScale
    const dy = (v / speed * NORMALIZED_WIND_ARROW_LENGTH_METERS) / latitudeScale
    const end: [number, number] = [lon + dx, lat + dy]
    const bodyLength = Math.hypot(dx, dy)
    const ux = dx / bodyLength
    const uy = dy / bodyLength
    const headLength = bodyLength * 0.42
    const headWidth = headLength * 0.65
    const left: [number, number] = [end[0] - ux * headLength - uy * headWidth, end[1] - uy * headLength + ux * headWidth]
    const right: [number, number] = [end[0] - ux * headLength + uy * headWidth, end[1] - uy * headLength - ux * headWidth]
    const windTo = (Math.atan2(u, v) * 180) / Math.PI
    const windToDeg = (windTo + 360) % 360
    return [{
      type: 'Feature',
      geometry: { type: 'MultiLineString', coordinates: [[[lon, lat], end], [end, left], [end, right]] },
      properties: {
        label: 'Wind TO',
        source: arrow.source ?? 'wind_diagnostics.map_arrows',
        time_utc: arrow.time_utc ?? null,
        speed_mps: Number(speed.toFixed(2)),
        wind_to_deg: Number(windToDeg.toFixed(1)),
        wind_from_deg: Number(((windToDeg + 180) % 360).toFixed(1)),
        u_east_mps: u,
        v_north_mps: v,
        display_length_km: NORMALIZED_WIND_ARROW_LENGTH_METERS / 1_000,
        display_length_normalized: true,
      },
    }]
  })
}

export const runSummaries = (detail: RunDetail): IncidentSummary[] =>
  (detail.summary.events ?? []).map((event: Record<string, any>) => ({
    incident_id: String(event.incident_id ?? event.event_id),
    event_id: String(event.event_id ?? ''),
    start_time: String(event.start_time ?? event.start_time_utc ?? ''),
    end_time: String(event.end_time ?? event.last_seen_time_utc ?? ''),
    duration_minutes: asNumber(event.duration_minutes),
    analysis_zone: String(event.analysis_zone ?? detail.manifest.analysis_zone ?? 'core'),
    event_strength: asNumber(event.event_strength),
    sensor_count: asNumber(event.sensor_count ?? event.unique_sensor_count),
    peak_pm25: asNumber(event.peak_pm25),
    max_anomaly_score: asNumber(event.max_anomaly_score),
    trace_status: String(event.trace_status ?? ''),
    evidence_status: String(event.evidence_status ?? ''),
    firms_status: String(event.firms_status ?? ''),
    cems_status: String(event.cems_status ?? ''),
    limitations: String(event.limitations ?? ''),
  }))

export const incidentFromDetail = (detail: IncidentDetail): IncidentSummary => {
  const event = detail.incident.event
  const evidence = detail.incident.evidence ?? {}
  const trace = detail.incident.trace ?? {}
  return {
    incident_id: detail.incident.incident_id,
    event_id: detail.incident.event_id,
    start_time: String(event.start_time_utc ?? ''),
    end_time: String(event.last_seen_time_utc ?? ''),
    duration_minutes: asNumber(event.duration_minutes),
    analysis_zone: String(detail.incident.analysis_zone ?? 'core'),
    event_strength: asNumber(event.event_strength),
    sensor_count: asNumber(event.unique_sensor_count),
    peak_pm25: asNumber(event.peak_pm25),
    max_anomaly_score: asNumber(event.max_anomaly_score),
    trace_status: String(trace.status ?? ''),
    evidence_status: String(evidence.status ?? ''),
    firms_status: String(evidence.firms_status ?? ''),
    cems_status: String(evidence.cems_status ?? ''),
    limitations: detail.incident.limitations.join('; '),
  }
}

export const formatTaipei = (value: string | null | undefined) => {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Taipei', hour: '2-digit', minute: '2-digit', day: '2-digit', month: 'short' }).format(date)
}

export const formatAge = (minutes: number | null | undefined) => {
  if (minutes == null) return 'No observation'
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${Math.round(minutes)} min ago`
  return `${(minutes / 60).toFixed(1)} h ago`
}

export const formatReplayWindow = (start: string | null | undefined, end: string | null | undefined) => {
  const format = (value: string | null | undefined) => {
    if (!value) return '—'
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return value
    const parts = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Taipei', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false }).formatToParts(date)
    const get = (type: string) => parts.find((part) => part.type === type)?.value ?? ''
    return { dayMonth: `${get('day')} ${get('month').replace('Sept', 'Sep')}`, time: `${get('hour')}:${get('minute')}` }
  }
  const from = format(start)
  const to = format(end)
  if (typeof from === 'string' || typeof to === 'string') return `${typeof from === 'string' ? from : `${from.dayMonth} ${from.time}`}–${typeof to === 'string' ? to : `${to.dayMonth} ${to.time}`}`
  return from.dayMonth === to.dayMonth ? `${from.dayMonth} ${from.time}–${to.time}` : `${from.dayMonth} ${from.time}–${to.dayMonth} ${to.time}`
}

export const stageLabel = (stage: string) => ({ anomaly: 'Analyzing sensors', events: 'Clustering event', wind_diagnostics: 'Estimating wind', backtrace: 'Tracing source', evidence: 'Matching evidence', complete: 'Complete' }[stage] ?? stage)

export const isSynthetic = (run: RunIndex | undefined) => Boolean(run?.synthetic_validation)
