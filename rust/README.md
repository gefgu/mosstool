# Rust Overture Map Pipeline

This workspace contains the Rust implementation for the Overture Maps based map
pipeline. It is intentionally separate from the existing Python package while
the port is in progress.

## Build a Map JSON

```bash
cargo run -p mosstool -- build-map \
  --name demo \
  --min-lon 116.32 \
  --min-lat 39.78 \
  --max-lon 116.40 \
  --max-lat 39.92 \
  --overture-release 2026-06-17.0 \
  --format json \
  --output data/temp/overture-map.json
```

The command queries Overture GeoParquet through DuckDB `spatial` and `httpfs`,
then writes a MOSS-shaped map JSON with `header`, `lanes`, `roads`,
`junctions`, `aois`, `pois`, and `_sublines`.

## Extract Source Data

```bash
cargo run -p mosstool -- extract-roadnet --min-lon 116.32 --min-lat 39.78 --max-lon 116.40 --max-lat 39.92 --output data/temp/roads.json
cargo run -p mosstool -- extract-aois --min-lon 116.32 --min-lat 39.78 --max-lon 116.40 --max-lat 39.92 --output data/temp/aois.json
cargo run -p mosstool -- extract-pois --min-lon 116.32 --min-lat 39.78 --max-lon 116.40 --max-lat 39.92 --output data/temp/pois.json
```

## Current Limits

- Protobuf output is not enabled yet because this repository does not include
  the CityProto `.proto` definitions needed by `prost-build`.
- The first implementation focuses on the map pipeline only. Trip generation,
  SUMO/GMNS conversion, and public transport post-processing remain in Python.
- Road topology is inferred from shared segment endpoints. Overture connector
  references should be used in the next pass for richer junction behavior.
