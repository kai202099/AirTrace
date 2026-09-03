# AirTrace attributions

AirTrace uses the following external data providers and services. These links
identify the source and official provider pages; they are not a substitute for
reviewing each provider's current terms before deployment or redistribution.

| Source | Use | Official source |
| --- | --- | --- |
| Taiwan Ministry of Environment (MOENV) | Micro-sensor SensorThings observations, reference air quality, facility data, and CEMS context | [MOENV open data](https://data.moenv.gov.tw/) · [Air quality SensorThings endpoint](https://sta.colife.org.tw/STA_AirQuality_EPAIoT/v1.0/) |
| Central Weather Administration (CWA) | Weather observations and wind fields | [CWA open data](https://opendata.cwa.gov.tw/) |
| NASA FIRMS | VIIRS NOAA-20 / NOAA-21 fire-hotspot context | [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/) |
| OpenStreetMap | Map tiles and map attribution in the web UI and generated diagnostics | [OpenStreetMap copyright and attribution](https://www.openstreetmap.org/copyright) · [tile service](https://tile.openstreetmap.org/) |

The frontend also uses the open-source packages listed in
[`frontend/package.json`](frontend/package.json), including React, React DOM,
MapLibre GL JS, Vite, TypeScript, Vitest, and jsdom. Their package metadata and
licenses are retained in `frontend/package-lock.json`; consult those upstream
projects for their current terms.
