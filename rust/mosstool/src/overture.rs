use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{anyhow, bail, Context, Result};
use duckdb::{Connection, Row};
use geojson::{GeoJson, Value};
use serde::{Deserialize, Serialize};

use crate::projection::LonLat;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
pub struct Bbox {
    pub min_lon: f64,
    pub min_lat: f64,
    pub max_lon: f64,
    pub max_lat: f64,
}

impl Bbox {
    pub fn center(self) -> LonLat {
        LonLat {
            lon: 0.5 * (self.min_lon + self.max_lon),
            lat: 0.5 * (self.min_lat + self.max_lat),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RoadSegment {
    pub id: String,
    pub name: String,
    pub class: String,
    pub geometry: Vec<LonLat>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Building {
    pub id: String,
    pub name: String,
    pub subtype: String,
    pub class: String,
    pub rings: Vec<Vec<LonLat>>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Place {
    pub id: String,
    pub name: String,
    pub category: String,
    pub confidence: Option<f64>,
    pub position: LonLat,
}

#[derive(Debug, Clone)]
pub struct OvertureConfig {
    pub release: String,
    pub bbox: Bbox,
}

pub struct OvertureClient {
    conn: Connection,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceCachePolicy {
    Read,
    Write,
    Refresh,
}

#[derive(Debug, Clone)]
pub struct SourceCache {
    pub dir: PathBuf,
    pub policy: SourceCachePolicy,
}

impl SourceCache {
    pub fn new(dir: PathBuf, policy: SourceCachePolicy) -> Self {
        Self { dir, policy }
    }

    fn roads_path(&self) -> PathBuf {
        self.dir.join("roads.parquet")
    }

    fn buildings_path(&self) -> PathBuf {
        self.dir.join("buildings.parquet")
    }

    fn places_path(&self) -> PathBuf {
        self.dir.join("places.parquet")
    }
}

impl OvertureClient {
    pub fn open() -> Result<Self> {
        let conn = Connection::open_in_memory().context("opening in-memory DuckDB")?;
        conn.execute_batch(
            r#"
            INSTALL spatial;
            INSTALL httpfs;
            LOAD spatial;
            LOAD httpfs;
            SET s3_region='us-west-2';
            "#,
        )
        .context("loading DuckDB spatial/httpfs extensions")?;
        Ok(Self { conn })
    }

    pub fn query_road_segments(&self, cfg: &OvertureConfig) -> Result<Vec<RoadSegment>> {
        let mut stmt = self
            .conn
            .prepare(&road_segments_sql(cfg))
            .context("preparing Overture road segment query")?;
        let rows = stmt.query_map([], road_segment_from_row)?;
        collect_rows(rows)
    }

    pub fn query_road_segments_cached(
        &self,
        cfg: &OvertureConfig,
        cache: Option<&SourceCache>,
    ) -> Result<Vec<RoadSegment>> {
        let Some(cache) = cache else {
            return self.query_road_segments(cfg);
        };
        let path = cache.roads_path();
        self.ensure_cache_file(
            &path,
            cache.policy,
            || road_segments_sql(cfg),
            "road segment source cache",
        )?;
        let mut stmt = self
            .conn
            .prepare(&road_segments_cache_sql(&path)?)
            .with_context(|| {
                format!(
                    "preparing cached road segment query from {}",
                    path.display()
                )
            })?;
        let rows = stmt.query_map([], road_segment_from_row)?;
        collect_rows(rows)
    }

    pub fn query_buildings(&self, cfg: &OvertureConfig) -> Result<Vec<Building>> {
        let mut stmt = self
            .conn
            .prepare(&buildings_sql(cfg))
            .context("preparing Overture building query")?;
        let rows = stmt.query_map([], building_from_row)?;
        collect_rows(rows)
    }

    pub fn query_buildings_cached(
        &self,
        cfg: &OvertureConfig,
        cache: Option<&SourceCache>,
    ) -> Result<Vec<Building>> {
        let Some(cache) = cache else {
            return self.query_buildings(cfg);
        };
        let path = cache.buildings_path();
        self.ensure_cache_file(
            &path,
            cache.policy,
            || buildings_sql(cfg),
            "building source cache",
        )?;
        let mut stmt = self
            .conn
            .prepare(&buildings_cache_sql(&path)?)
            .with_context(|| format!("preparing cached building query from {}", path.display()))?;
        let rows = stmt.query_map([], building_from_row)?;
        collect_rows(rows)
    }

    pub fn query_places(&self, cfg: &OvertureConfig, confidence: f64) -> Result<Vec<Place>> {
        let mut stmt = self
            .conn
            .prepare(&places_sql(cfg, confidence))
            .context("preparing Overture place query")?;
        let rows = stmt.query_map([], place_from_row)?;
        collect_rows(rows)
    }

    pub fn query_places_cached(
        &self,
        cfg: &OvertureConfig,
        confidence: f64,
        cache: Option<&SourceCache>,
    ) -> Result<Vec<Place>> {
        let Some(cache) = cache else {
            return self.query_places(cfg, confidence);
        };
        let path = cache.places_path();
        self.ensure_cache_file(
            &path,
            cache.policy,
            || places_sql(cfg, confidence),
            "place source cache",
        )?;
        let mut stmt = self
            .conn
            .prepare(&places_cache_sql(&path)?)
            .with_context(|| format!("preparing cached place query from {}", path.display()))?;
        let rows = stmt.query_map([], place_from_row)?;
        collect_rows(rows)
    }

    fn ensure_cache_file<F>(
        &self,
        path: &Path,
        policy: SourceCachePolicy,
        source_sql: F,
        label: &str,
    ) -> Result<()>
    where
        F: FnOnce() -> String,
    {
        match policy {
            SourceCachePolicy::Read => {
                if !path.is_file() {
                    bail!("missing {label}: {}", path.display());
                }
            }
            SourceCachePolicy::Write if path.is_file() => {}
            SourceCachePolicy::Write | SourceCachePolicy::Refresh => {
                if let Some(parent) = path.parent() {
                    fs::create_dir_all(parent).with_context(|| {
                        format!("creating source cache directory {}", parent.display())
                    })?;
                }
                let sql = copy_to_parquet_sql(&source_sql(), path)?;
                self.conn
                    .execute_batch(&sql)
                    .with_context(|| format!("writing {label} to {}", path.display()))?;
            }
        }
        Ok(())
    }
}

fn collect_rows<T>(
    rows: duckdb::MappedRows<'_, impl FnMut(&Row<'_>) -> duckdb::Result<T>>,
) -> Result<Vec<T>> {
    rows.collect::<duckdb::Result<Vec<_>>>()
        .map_err(|err| anyhow!(err))
}

pub fn road_segments_sql(cfg: &OvertureConfig) -> String {
    let b = cfg.bbox;
    format!(
        r#"
        SELECT
            id,
            COALESCE(names.primary, '') AS name,
            COALESCE(class, '') AS class,
            ST_AsGeoJSON(geometry) AS geometry_json
        FROM read_parquet('s3://overturemaps-us-west-2/release/{release}/theme=transportation/type=segment/*', filename=true, hive_partitioning=1)
        WHERE bbox.xmin < {max_lon}
          AND bbox.ymin < {max_lat}
          AND bbox.xmax > {min_lon}
          AND bbox.ymax > {min_lat}
          AND geometry IS NOT NULL
        ORDER BY id
        "#,
        release = cfg.release,
        min_lon = b.min_lon,
        min_lat = b.min_lat,
        max_lon = b.max_lon,
        max_lat = b.max_lat,
    )
}

pub fn buildings_sql(cfg: &OvertureConfig) -> String {
    let b = cfg.bbox;
    format!(
        r#"
        SELECT
            id,
            COALESCE(names.primary, '') AS name,
            COALESCE(subtype, '') AS subtype,
            COALESCE(class, '') AS class,
            ST_AsGeoJSON(geometry) AS geometry_json
        FROM read_parquet('s3://overturemaps-us-west-2/release/{release}/theme=buildings/type=building/*', filename=true, hive_partitioning=1)
        WHERE bbox.xmin < {max_lon}
          AND bbox.ymin < {max_lat}
          AND bbox.xmax > {min_lon}
          AND bbox.ymax > {min_lat}
          AND geometry IS NOT NULL
        ORDER BY id
        "#,
        release = cfg.release,
        min_lon = b.min_lon,
        min_lat = b.min_lat,
        max_lon = b.max_lon,
        max_lat = b.max_lat,
    )
}

pub fn places_sql(cfg: &OvertureConfig, confidence: f64) -> String {
    let b = cfg.bbox;
    format!(
        r#"
        SELECT
            id,
            COALESCE(names.primary, '') AS name,
            COALESCE(basic_category, COALESCE(categories.primary, 'unknown')) AS category,
            confidence,
            ST_AsGeoJSON(geometry) AS geometry_json
        FROM read_parquet('s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*', filename=true, hive_partitioning=1)
        WHERE bbox.xmin BETWEEN {min_lon} AND {max_lon}
          AND bbox.ymin BETWEEN {min_lat} AND {max_lat}
          AND COALESCE(confidence, 1.0) >= {confidence}
          AND COALESCE(operating_status, 'open') != 'permanently_closed'
          AND geometry IS NOT NULL
        ORDER BY id
        "#,
        release = cfg.release,
        min_lon = b.min_lon,
        min_lat = b.min_lat,
        max_lon = b.max_lon,
        max_lat = b.max_lat,
        confidence = confidence,
    )
}

fn road_segments_cache_sql(path: &Path) -> Result<String> {
    Ok(format!(
        r#"
        SELECT id, name, class, geometry_json
        FROM read_parquet({})
        ORDER BY id
        "#,
        sql_string_literal(path)?,
    ))
}

fn buildings_cache_sql(path: &Path) -> Result<String> {
    Ok(format!(
        r#"
        SELECT id, name, subtype, class, geometry_json
        FROM read_parquet({})
        ORDER BY id
        "#,
        sql_string_literal(path)?,
    ))
}

fn places_cache_sql(path: &Path) -> Result<String> {
    Ok(format!(
        r#"
        SELECT id, name, category, confidence, geometry_json
        FROM read_parquet({})
        ORDER BY id
        "#,
        sql_string_literal(path)?,
    ))
}

fn copy_to_parquet_sql(source_sql: &str, path: &Path) -> Result<String> {
    Ok(format!(
        "COPY ({source_sql}) TO {} (FORMAT PARQUET)",
        sql_string_literal(path)?,
    ))
}

fn sql_string_literal(path: &Path) -> Result<String> {
    let value = path
        .to_str()
        .with_context(|| format!("path is not valid UTF-8: {}", path.display()))?;
    Ok(format!("'{}'", value.replace('\'', "''")))
}

fn road_segment_from_row(row: &Row<'_>) -> duckdb::Result<RoadSegment> {
    let geometry_json: String = row.get(3)?;
    Ok(RoadSegment {
        id: row.get(0)?,
        name: row.get(1)?,
        class: row.get(2)?,
        geometry: parse_linestring(&geometry_json).map_err(to_duckdb_conversion_error)?,
    })
}

fn building_from_row(row: &Row<'_>) -> duckdb::Result<Building> {
    let geometry_json: String = row.get(4)?;
    Ok(Building {
        id: row.get(0)?,
        name: row.get(1)?,
        subtype: row.get(2)?,
        class: row.get(3)?,
        rings: parse_polygon_rings(&geometry_json).map_err(to_duckdb_conversion_error)?,
    })
}

fn place_from_row(row: &Row<'_>) -> duckdb::Result<Place> {
    let geometry_json: String = row.get(4)?;
    Ok(Place {
        id: row.get(0)?,
        name: row.get(1)?,
        category: row.get(2)?,
        confidence: row.get(3)?,
        position: parse_point(&geometry_json).map_err(to_duckdb_conversion_error)?,
    })
}

fn to_duckdb_conversion_error(err: anyhow::Error) -> duckdb::Error {
    duckdb::Error::ToSqlConversionFailure(Box::new(std::io::Error::new(
        std::io::ErrorKind::InvalidData,
        err.to_string(),
    )))
}

pub fn parse_point(json: &str) -> Result<LonLat> {
    let geojson = json.parse::<GeoJson>()?;
    match geojson {
        GeoJson::Geometry(geometry) => match geometry.value {
            Value::Point(coord) if coord.len() >= 2 => Ok(LonLat {
                lon: coord[0],
                lat: coord[1],
            }),
            _ => Err(anyhow!("expected GeoJSON Point geometry")),
        },
        _ => Err(anyhow!("expected GeoJSON geometry")),
    }
}

pub fn parse_linestring(json: &str) -> Result<Vec<LonLat>> {
    let geojson = json.parse::<GeoJson>()?;
    match geojson {
        GeoJson::Geometry(geometry) => match geometry.value {
            Value::LineString(coords) => coords_to_lonlat(coords),
            _ => Err(anyhow!("expected GeoJSON LineString geometry")),
        },
        _ => Err(anyhow!("expected GeoJSON geometry")),
    }
}

pub fn parse_polygon_rings(json: &str) -> Result<Vec<Vec<LonLat>>> {
    let geojson = json.parse::<GeoJson>()?;
    match geojson {
        GeoJson::Geometry(geometry) => match geometry.value {
            Value::Polygon(rings) => rings.into_iter().map(coords_to_lonlat).collect(),
            Value::MultiPolygon(polygons) => polygons
                .into_iter()
                .filter_map(|poly| poly.into_iter().next())
                .map(coords_to_lonlat)
                .collect(),
            _ => Err(anyhow!("expected GeoJSON Polygon or MultiPolygon geometry")),
        },
        _ => Err(anyhow!("expected GeoJSON geometry")),
    }
}

fn coords_to_lonlat(coords: Vec<Vec<f64>>) -> Result<Vec<LonLat>> {
    coords
        .into_iter()
        .map(|coord| {
            if coord.len() < 2 {
                return Err(anyhow!("coordinate has fewer than two dimensions"));
            }
            Ok(LonLat {
                lon: coord[0],
                lat: coord[1],
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> OvertureConfig {
        OvertureConfig {
            release: "2026-06-17.0".to_string(),
            bbox: Bbox {
                min_lon: 1.0,
                min_lat: 2.0,
                max_lon: 3.0,
                max_lat: 4.0,
            },
        }
    }

    #[test]
    fn builds_transportation_query() {
        let sql = road_segments_sql(&cfg());
        assert!(sql.contains("theme=transportation/type=segment"));
        assert!(sql.contains("release/2026-06-17.0"));
        assert!(sql.contains("bbox.xmin < 3"));
    }

    #[test]
    fn builds_cache_queries_with_escaped_paths() {
        let path = Path::new("/tmp/moss'tool/roads.parquet");
        let sql = road_segments_cache_sql(path).unwrap();
        assert!(sql.contains("read_parquet('/tmp/moss''tool/roads.parquet')"));

        let copy_sql = copy_to_parquet_sql("SELECT 1 AS id", path).unwrap();
        assert!(copy_sql.contains("COPY (SELECT 1 AS id) TO '/tmp/moss''tool/roads.parquet'"));
        assert!(copy_sql.contains("FORMAT PARQUET"));
    }

    #[test]
    fn parses_geojson_geometries() {
        let line =
            parse_linestring(r#"{"type":"LineString","coordinates":[[1,2],[3,4]]}"#).unwrap();
        assert_eq!(line[0], LonLat { lon: 1.0, lat: 2.0 });

        let point = parse_point(r#"{"type":"Point","coordinates":[5,6]}"#).unwrap();
        assert_eq!(point, LonLat { lon: 5.0, lat: 6.0 });

        let rings =
            parse_polygon_rings(r#"{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,0]]]}"#)
                .unwrap();
        assert_eq!(rings.len(), 1);
    }
}
