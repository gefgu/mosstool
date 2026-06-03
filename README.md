# mosstool

MObility Simulation System (MOSS) Toolbox

## Highlights

- Builds road networks, buildings, AOIs, POIs, and trip inputs for MOSS simulations.
- Uses DuckDB spatial operations for POI-to-AOI matching, covered-AOI merging, and AOI-to-lane matching, improving scalability on large maps.
- Matches AOIs to driving and walking lanes with stable global indexing, reducing incorrect lane assignments during batched processing.
- Supports POI generation from OSM and Overture Maps, with H3/name/address-based merging to reduce duplicate POIs.

## Installation

```bash
pip install mosstool
```

More basic concept introductions and tutorials are available at [MOSS](https://moss.fiblab.net/docs/introduction)

GitHub Repo: [https://github.com/tsinghua-fib-lab/mosstool](https://github.com/tsinghua-fib-lab/mosstool)
