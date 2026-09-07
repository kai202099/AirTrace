import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true

const mapState = vi.hoisted(() => ({
  handlers: new Map<string, (event: any) => void>(),
}))

vi.mock('maplibre-gl', () => {
  class FakeMap {
    private layers = new Set<string>()
    private sources = new Map<string, { setData: (data: unknown) => void }>()

    addControl() {}
    remove() {}
    getCanvas() { return { style: { cursor: '' } } }
    getLayer(id: string) { return this.layers.has(id) ? { id } : undefined }
    addLayer(layer: { id: string }) { this.layers.add(layer.id) }
    setLayoutProperty() {}
    getSource(id: string) { return this.sources.get(id) }
    addSource(id: string) { this.sources.set(id, { setData: () => undefined }) }
    on(event: string, layerOrHandler: string | ((event: any) => void), handler?: (event: any) => void) {
      if (typeof layerOrHandler === 'function') layerOrHandler({})
      else if (handler) mapState.handlers.set(`${event}:${layerOrHandler}`, handler)
    }
    off(event: string, layer: string) { mapState.handlers.delete(`${event}:${layer}`) }
  }

  return {
    default: {
      Map: FakeMap,
      NavigationControl: class {},
      AttributionControl: class {},
      ScaleControl: class {},
    },
  }
})

const run = {
  run_id: 'run-1',
  mode: 'LIVE_ANALYSIS',
  diagnostic: false,
  synthetic_validation: false,
  analysis_start_utc: '2026-09-02T12:00:00Z',
  analysis_end_utc: '2026-09-02T13:00:00Z',
  analysis_zone: 'core',
  event_count: 1,
  message: null,
  warnings: [],
  stage_status: {},
}

const runs = Array.from({ length: 10 }, (_, index) => ({
  ...run,
  run_id: `run-${index + 1}`,
}))

const incident = {
  incident: {
    incident_id: 'incident-1',
    event_id: 'event-1',
    analysis_zone: 'core',
    event: {
      start_time_utc: '2026-09-02T12:10:00Z',
      last_seen_time_utc: '2026-09-02T12:20:00Z',
      duration_minutes: 10,
      unique_sensor_count: 1,
      peak_pm25: 42,
      max_anomaly_score: 3,
      event_strength: 0.7,
      latest_centroid: { lon: 121.46, lat: 25.06 },
    },
    wind: {},
    trace: { status: 'TRACE_COMPLETE', candidate_source_regions: [] },
    evidence: {},
    limitations: [],
  },
  membership: [],
  source_evidence: [],
  facilities: [],
  fires: [],
}

vi.mock('./api', () => ({
  createAnalysis: vi.fn(),
  getIncident: vi.fn(async () => ({ ...incident, run, manifest: {} })),
  getJob: vi.fn(),
  getLive: vi.fn(async () => ({
    as_of_utc: '2026-09-02T13:00:00Z',
    region: {
      context_bbox: { west: 121.3, south: 24.9, east: 121.6, north: 25.2 },
      core_bbox: { west: 121.4, south: 25, east: 121.5, north: 25.1 },
    },
    sensors: { status: 'OK', sensors: [], counts: {} },
    weather: { status: 'OK', stations: [], summary: null },
    latest_run: run,
  })),
  getRun: vi.fn(async () => ({
    manifest: run,
    summary: {
      event_count: 1,
      events: [{
        incident_id: 'incident-1',
        event_id: 'event-1',
        start_time: '2026-09-02T12:10:00Z',
        end_time: '2026-09-02T12:20:00Z',
        duration_minutes: 10,
        analysis_zone: 'core',
        event_strength: 0.7,
        sensor_count: 1,
        peak_pm25: 42,
        trace_status: 'TRACE_COMPLETE',
      }],
    },
    incidents: [incident],
  })),
  getRuns: vi.fn(async () => runs),
  getStatus: vi.fn(async () => ({})),
}))

import App from './App'
import { getRun } from './api'

const flushEffects = async () => {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })
}

describe('App map evidence selection', () => {
  let container: HTMLDivElement
  let root: ReturnType<typeof createRoot>

  beforeEach(() => {
    mapState.handlers.clear()
    vi.clearAllMocks()
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => root.unmount())
    container.remove()
  })

  it('lets users reveal and select runs after the initial eight', async () => {
    await act(async () => root.render(<App />))
    await flushEffects()

    const replayButton = [...container.querySelectorAll<HTMLButtonElement>('.mode-nav button')]
      .find((button) => button.textContent?.includes('REPLAY'))
    await act(async () => replayButton!.click())

    expect(container.querySelectorAll('.run-card')).toHaveLength(8)
    const showMore = container.querySelector<HTMLButtonElement>('.run-history-toggle')
    expect(showMore?.textContent).toBe('Show 2 more')
    expect(showMore?.getAttribute('aria-expanded')).toBe('false')

    await act(async () => showMore!.click())

    expect(container.querySelectorAll('.run-card')).toHaveLength(10)
    expect(showMore?.textContent).toBe('Show fewer')
    expect(showMore?.getAttribute('aria-expanded')).toBe('true')

    const tenthRun = container.querySelectorAll<HTMLButtonElement>('.run-card')[9]
    await act(async () => tenthRun.click())
    await flushEffects()

    expect(getRun).toHaveBeenCalledWith('run-10')
    expect(container.querySelector('.replay-view')).toBeNull()
  })

  it.each([
    {
      kind: 'sensor',
      handler: 'click:sensor-points',
      properties: { station_id: 'S-1', freshness: 'fresh', pm25: 23 },
      inspectorText: 'S-1',
    },
    {
      kind: 'facility',
      handler: 'click:facility-points',
      properties: { map_label: 'F1', name: 'Candidate facility' },
      inspectorText: 'Candidate facility',
    },
  ])('keeps the incident context and layers when a $kind is inspected', async ({ handler, properties, inspectorText }) => {
    await act(async () => root.render(<App />))
    await flushEffects()
    await flushEffects()

    const incidentButton = container.querySelector<HTMLButtonElement>('.incident-row')
    expect(incidentButton).not.toBeNull()
    await act(async () => incidentButton!.click())
    await flushEffects()

    expect(incidentButton!.classList.contains('selected')).toBe(true)
    expect(mapState.handlers.get(handler)).toBeTypeOf('function')

    await act(async () => mapState.handlers.get(handler)!({
      features: [{ properties }],
    }))

    expect(container.textContent).toContain(inspectorText)
    expect(incidentButton!.classList.contains('selected')).toBe(true)
    const eventsLayer = [...container.querySelectorAll<HTMLButtonElement>('.map-controls button')]
      .find((button) => button.textContent === 'Events')
    expect(eventsLayer?.disabled).toBe(false)
  })
})
