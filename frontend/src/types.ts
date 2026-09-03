export type Mode = 'LIVE' | 'REPLAY'

export type Sensor = {
  station_id: string
  station_name?: string
  lat: number
  lon: number
  pm25: number | null
  timestamp_utc: string | null
  freshness: 'fresh' | 'stale' | 'offline' | 'future_timestamp'
  age_minutes: number | null
  source_status?: string
  quality_flags?: string
}

export type RunIndex = {
  run_id: string
  mode: string
  diagnostic: boolean
  synthetic_validation: boolean
  analysis_start_utc: string
  analysis_end_utc: string
  analysis_zone: string
  event_count: number
  message: string | null
  warnings: string[]
  stage_status: Record<string, string>
}

export type IncidentSummary = {
  incident_id: string
  event_id: string
  start_time: string
  end_time: string
  duration_minutes: number
  analysis_zone: string
  event_strength: number
  sensor_count: number
  peak_pm25: number
  max_anomaly_score: number
  trace_status: string
  evidence_status: string
  firms_status: string
  cems_status: string
  limitations: string
}

export type IncidentDetail = {
  incident: {
    incident_id: string
    event_id: string
    analysis_zone?: string
    event: Record<string, any>
    wind: Record<string, any>
    trace: Record<string, any>
    evidence: Record<string, any>
    limitations: string[]
  }
  membership: Record<string, string>[]
  source_evidence: Record<string, string>[]
  facilities: Record<string, string>[]
  fires: Record<string, string>[]
}

export type RunDetail = { manifest: Record<string, any>; summary: Record<string, any>; incidents: IncidentDetail[] }

export type LivePayload = {
  as_of_utc: string
  region: Record<string, any>
  sensors: { status: string; sensors: Sensor[]; counts: Record<string, number>; pm25_median?: number | null }
  weather: { status: string; stations: Record<string, any>[]; summary: Record<string, any> | null }
  latest_run: RunIndex | null
  recent_runs: RunIndex[]
}

export type StatusPayload = Record<string, any>
