# AirTrace

AirTrace turns dense PM2.5 sensor observations, reconstructed wind fields,
backward particle tracing, and public facility/fire evidence into
evidence-supported candidate source regions.

It does **not** determine legal responsibility or prove that a facility caused
an event.

> Screenshot placeholder: capture the local workspace after starting the API
> and frontend, then add the image here for a public demo page.

## Why AirTrace

Taiwan's dense micro-air-sensor network can reveal local PM2.5 patterns, but a
concentration spike alone does not identify a source. AirTrace combines
spatial and temporal sensor signals with reconstructed weather, backward
particle tracing, and public facility, CEMS, and fire evidence to show where a
candidate source region is supported and where uncertainty remains.

The current pilot region is the Wugu/Xinzhuang context around the configured
Core Zone; it is not a nationwide attribution service.

## How it works

```text
PM2.5
  → anomaly detection
  → event clustering
  → validated wind field
  → backward particle tracing
  → relative source evidence
  → facility / FIRMS / CEMS context
```

The analysis core is intentionally frozen for this release. This repository
patch changes public packaging, documentation, provenance, and portability,
not anomaly thresholds, clustering, wind interpolation, backtrace,
evidence-scoring, recorder, pipeline, or known-fire calculations.

## Modes

- **LIVE** — reads the local recorder databases and shows the latest available
  observations and live analysis state.
- **REPLAY** — runs the existing pipeline against a bounded historical window.
  The browser does not analyze PM2.5 itself.
- **DEMO — SYNTHETIC VALIDATION** — loads the curated deterministic fixture in
  [`fixtures/demo/synthetic_full_event/`](fixtures/demo/synthetic_full_event/).
  It is synthetic validation data, not a real pollution event or a historical
  observation. Its warning comes from explicit manifest provenance metadata,
  not from its directory name.

## Validation

The project includes the following validation artifacts and scripts:

- wind-field leave-one-station-out validation;
- deterministic synthetic source recovery;
- real known-fire blind replay;
- known-fire post-hoc sensor and spatial diagnostics.

The known-fire reports use approximate reported landmark locations as
post-hoc validation references. The blind replay was run without ground-truth
coordinates. These references do not prove exact fire origins or attribution.
The system did not force known fires into detections when the available
evidence was insufficient, and these diagnostics do not establish attribution
success or a general accuracy rate.

## Data sources

- [MOENV open data](https://data.moenv.gov.tw/) and the [MOENV air-quality
  SensorThings endpoint](https://sta.colife.org.tw/STA_AirQuality_EPAIoT/v1.0/)
  for Taiwan micro sensors, reference air quality, and facility context.
- [CWA open data](https://opendata.cwa.gov.tw/) for weather observations.
- [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/) VIIRS NOAA-20 / NOAA-21
  for fire-hotspot context.
- [OpenStreetMap](https://www.openstreetmap.org/copyright) tiles for maps.

See [`ATTRIBUTIONS.md`](ATTRIBUTIONS.md) for provider and frontend-library
attribution notes.

## Quick Start

AirTrace supports Python 3.11 or newer and uses the Node version required by
Vite 7: Node `^20.19.0 || >=22.12.0`.

### Backend

From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

Copy `.env.example` to `.env` and add local credentials only. Never commit
`.env` or real keys.

Start the local API:

```powershell
uvicorn airtrace.api.app:app --reload
```

Recorders and bootstrap scripts write local DuckDB databases and ignored raw
snapshots. Poll once while setting up, or omit `--once` for the recorder loop:

```powershell
python scripts/record_pm25.py --once
python scripts/record_weather.py --once
python scripts/fetch_facilities.py
python scripts/fetch_reference_air.py --once
python scripts/fetch_cems.py --year-month YYYY-MM
```

Collect sufficient history before attempting a meaningful live analysis. A
fresh clone has no historical live observations.

### Frontend

In a second terminal:

```powershell
cd frontend
npm ci
npm run test
npm run build
npm run dev
```

Open the Vite URL shown in the terminal. The default API proxy targets
`http://127.0.0.1:8000`. To see an immediately reproducible full-pipeline
demonstration, start both services and choose **REPLAY**, then select
**Synthetic validation scenario**.

## Environment variables

All values below are optional for serving the curated synthetic fixture, but
are needed for the corresponding live/bootstrap capability:

- `MOENV_API_KEY` — required by MOENV-backed facility, CEMS, and reference-air
  fetches.
- `CWA_API_KEY` — required by the CWA weather recorder.
- `FIRMS_MAP_KEY` — optional; enables NASA FIRMS queries. If missing, AirTrace
  records that FIRMS was not queried and does not treat that as a fire absence.
- `AIRTRACE_CORS_ORIGINS` — optional comma-separated browser origins. The
  default is `http://localhost:5173,http://127.0.0.1:5173`; it is never `*` by
  default.

`.env.example` contains placeholders only. Credentials are loaded for the
local Python process and are not returned in API status or persisted manifests.

## Live data and reproducibility

A fresh clone intentionally does **not** contain runtime DuckDB databases or
raw downloaded observations. To reproduce a live analysis, configure the APIs,
run the recorders or data bootstrap scripts, collect sufficient history, and
run the pipeline. The curated synthetic fixture is provided for an immediate,
deterministic UI/full-pipeline demonstration; the repository alone does not
contain historical live observations.

## Testing

From the repository root:

```powershell
python -m pytest
python -m compileall -q airtrace scripts tests
cd frontend
npm ci
npm run test
npm run build
```

The Python test-only dependencies are listed in `requirements-dev.txt`.

## Limitations

- Micro-sensor observations have calibration, placement, and quality
  uncertainty.
- Weather interpolation and backward tracing inherit uncertainty from station
  coverage, temporal availability, and wind disagreement.
- Relative source evidence is not a probability.
- The current CEMS snapshot can be partial. CEMS is supporting context only,
  does not modify core facility evidence ranking, and no completeness claim is
  made.
- FIRMS non-detection does not rule out small, obscured, or below-detection
  fires.
- Candidate facilities and candidate source regions are not attribution.
- Known-fire landmarks are approximate reported locations, not exact origins.
- The pilot region currently covers Wugu/Xinzhuang context rather than all of
  Taiwan.

## Public-hosting scope

The included FastAPI service is intended for local/demo use or a trusted
deployment. Authentication and rate limiting are intentionally out of scope.
`POST /api/analyze` performs computationally expensive local analysis and
should not be exposed directly to an untrusted public internet without
authentication and rate limiting. This MVP is not a multi-user SaaS backend.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
