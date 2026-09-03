import type { IncidentDetail, IncidentSummary, RunDetail, RunIndex } from './types'

const asNumber = (value: unknown) => {
  const number = Number(value)
  return Number.isFinite(number) ? number : 0
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

export const isSynthetic = (run: RunIndex | undefined) => Boolean(run?.synthetic_validation || run?.run_id?.toLowerCase().includes('synthetic'))
