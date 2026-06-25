# mosstool

MObility Simulation System (MOSS) Toolbox

## My Highlights

- Builds road networks, buildings, AOIs, POIs, and trip inputs for MOSS simulations.
- Uses DuckDB spatial operations for POI-to-AOI matching, covered-AOI merging, and AOI-to-lane matching, improving scalability on large maps.
- Matches AOIs to driving and walking lanes with stable global indexing, reducing incorrect lane assignments during batched processing.
- Supports POI generation from OSM and Overture Maps, with H3/name/address-based merging to reduce duplicate POIs.

## Rust Overture Map Pipeline

In addition to the Python package, this repository includes an experimental Rust
workspace under [`rust/`](rust/) that ports the Overture Maps based map pipeline.
It runs independently of the Python code while the port is in progress.

- Queries Overture GeoParquet directly through DuckDB (`spatial` + `httpfs`) and
  writes a MOSS-shaped map JSON (`header`, `lanes`, `roads`, `junctions`,
  `aois`, `pois`, `_sublines`).
- Provides a CLI (`cargo run -p mosstool -- ...`) for building maps and
  extracting road network, AOI, and POI source data for a bounding box.
- Currently covers the map pipeline only — trip generation, SUMO/GMNS
  conversion, and public transport post-processing remain in the Python package.
  Protobuf output is not yet enabled.

See [`rust/README.md`](rust/README.md) for build commands and current limits.

## Installation

```bash
pip install mosstool
```

More basic concept introductions and tutorials are available at [MOSS](https://moss.fiblab.net/docs/introduction)

Original GitHub Repo: [https://github.com/tsinghua-fib-lab/mosstool](https://github.com/tsinghua-fib-lab/mosstool)
