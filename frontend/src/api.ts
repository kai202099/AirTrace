import type { IncidentDetail, LivePayload, RunDetail, RunIndex, StatusPayload } from './types'

const api = async <T>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...init })
  if (!response.ok) {
    const body = await response.text()
    throw new Error(body || `${response.status} ${response.statusText}`)
  }
  return response.json() as Promise<T>
}

export const getLive = () => api<LivePayload>('/api/live')
export const getStatus = () => api<StatusPayload>('/api/status')
export const getRuns = async () => (await api<{ runs: RunIndex[] }>('/api/runs')).runs
export const getRun = (runId: string) => api<RunDetail>(`/api/runs/${encodeURIComponent(runId)}`)
export const getIncident = (incidentId: string) => api<IncidentDetail & { run: RunIndex; manifest: Record<string, any> }>(`/api/incidents/${encodeURIComponent(incidentId)}`)
export const createAnalysis = (body: { start: string; end: string; analysis_zone: string; fast_preview: boolean }) => api<{ job_id: string }>('/api/analyze', { method: 'POST', body: JSON.stringify(body) })
export const getJob = (jobId: string) => api<{ status: string; stage: string; progress: number; error?: string; result?: { run_id: string; event_count: number } }>(`/api/jobs/${jobId}`)
