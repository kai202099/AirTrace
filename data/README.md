# Data directory

This directory separates small public snapshots from local runtime data.

- `normalized/pilot_sensor_inventory.csv` is a checked-in normalized sensor
  inventory snapshot used for inspection and documentation.
- `cems_ingest_metadata.json` is a checked-in ingest-status snapshot. A partial
  snapshot is not a completeness claim.
- `airtrace.duckdb`, `weather.duckdb`, `facilities.duckdb`,
  `reference_air.duckdb`, and `cems.duckdb` are runtime DuckDB databases and
  are ignored by Git.
- `raw/` contains downloaded provider payloads and is ignored by Git. It can
  grow quickly and should be retained locally only as long as needed for the
  analysis or reproducibility window.

A fresh clone intentionally does not contain the runtime databases or raw
observations. Configure the APIs and run the recorders/bootstrap scripts when
live data is required. The curated synthetic validation fixture under
`fixtures/demo/` is the immediately reproducible public demonstration.
