use std::fs::File;
use std::io::BufWriter;
use std::path::PathBuf;

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand, ValueEnum};
use mosstool::map::{build_map, BuildInput};
use mosstool::overture::{Bbox, OvertureClient, OvertureConfig, SourceCache, SourceCachePolicy};
use tracing_subscriber::EnvFilter;

#[derive(Debug, Parser)]
#[command(name = "mosstool")]
#[command(about = "Rust Overture Maps pipeline for MOSS map generation")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Build a MOSS-shaped map JSON file from Overture Maps.
    BuildMap(BuildMapArgs),
    /// Export Overture transportation segments as GeoJSON-like JSON rows.
    ExtractRoadnet(ExtractArgs),
    /// Export Overture building footprints as JSON rows.
    ExtractAois(ExtractArgs),
    /// Export Overture places as JSON rows.
    ExtractPois(ExtractPoiArgs),
}

#[derive(Debug, Parser)]
struct BuildMapArgs {
    #[arg(long)]
    name: String,
    #[arg(long)]
    min_lon: f64,
    #[arg(long)]
    min_lat: f64,
    #[arg(long)]
    max_lon: f64,
    #[arg(long)]
    max_lat: f64,
    #[arg(long)]
    projection: Option<String>,
    #[arg(long, default_value = "2026-06-17.0")]
    overture_release: String,
    #[arg(long, default_value_t = 0.8)]
    confidence: f64,
    #[arg(long)]
    source_cache_dir: Option<PathBuf>,
    #[arg(long, value_enum, default_value_t = SourceCachePolicyArg::Write)]
    source_cache_policy: SourceCachePolicyArg,
    #[arg(long)]
    prepare_cache_only: bool,
    #[arg(long)]
    output: Option<PathBuf>,
    #[arg(long, value_parser = ["json"], default_value = "json")]
    format: String,
}

#[derive(Debug, Parser)]
struct ExtractArgs {
    #[arg(long)]
    min_lon: f64,
    #[arg(long)]
    min_lat: f64,
    #[arg(long)]
    max_lon: f64,
    #[arg(long)]
    max_lat: f64,
    #[arg(long, default_value = "2026-06-17.0")]
    overture_release: String,
    #[arg(long)]
    output: PathBuf,
}

#[derive(Debug, Parser)]
struct ExtractPoiArgs {
    #[command(flatten)]
    base: ExtractArgs,
    #[arg(long, default_value_t = 0.8)]
    confidence: f64,
}

fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env())
        .init();

    match Cli::parse().command {
        Command::BuildMap(args) => build_map_command(args),
        Command::ExtractRoadnet(args) => extract_roadnet_command(args),
        Command::ExtractAois(args) => extract_aois_command(args),
        Command::ExtractPois(args) => extract_pois_command(args),
    }
}

fn build_map_command(args: BuildMapArgs) -> Result<()> {
    if args.format != "json" {
        bail!("only JSON output is implemented until CityProto .proto files are available");
    }
    let bbox = bbox_from_values(args.min_lon, args.min_lat, args.max_lon, args.max_lat)?;
    let projection = args.projection.unwrap_or_else(|| {
        let center = bbox.center();
        format!("+proj=tmerc +lat_0={} +lon_0={}", center.lat, center.lon)
    });
    let cfg = OvertureConfig {
        release: args.overture_release,
        bbox,
    };
    let client = OvertureClient::open()?;
    let cache = args
        .source_cache_dir
        .map(|dir| SourceCache::new(dir, args.source_cache_policy.into()));
    if args.prepare_cache_only && cache.is_none() {
        bail!("--prepare-cache-only requires --source-cache-dir");
    }
    let roads = client.query_road_segments_cached(&cfg, cache.as_ref())?;
    let buildings = client.query_buildings_cached(&cfg, cache.as_ref())?;
    let places = client.query_places_cached(&cfg, args.confidence, cache.as_ref())?;
    if args.prepare_cache_only {
        return Ok(());
    }
    let output_path = args
        .output
        .as_ref()
        .context("--output is required unless --prepare-cache-only is set")?;
    let output = build_map(BuildInput {
        name: args.name,
        projection,
        bbox,
        roads,
        buildings,
        places,
    });
    write_json(output_path, &output)
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum SourceCachePolicyArg {
    Read,
    Write,
    Refresh,
}

impl From<SourceCachePolicyArg> for SourceCachePolicy {
    fn from(value: SourceCachePolicyArg) -> Self {
        match value {
            SourceCachePolicyArg::Read => SourceCachePolicy::Read,
            SourceCachePolicyArg::Write => SourceCachePolicy::Write,
            SourceCachePolicyArg::Refresh => SourceCachePolicy::Refresh,
        }
    }
}

fn extract_roadnet_command(args: ExtractArgs) -> Result<()> {
    let cfg = cfg_from_extract_args(&args)?;
    let client = OvertureClient::open()?;
    let rows = client.query_road_segments(&cfg)?;
    write_json(&args.output, &rows)
}

fn extract_aois_command(args: ExtractArgs) -> Result<()> {
    let cfg = cfg_from_extract_args(&args)?;
    let client = OvertureClient::open()?;
    let rows = client.query_buildings(&cfg)?;
    write_json(&args.output, &rows)
}

fn extract_pois_command(args: ExtractPoiArgs) -> Result<()> {
    let cfg = cfg_from_extract_args(&args.base)?;
    let client = OvertureClient::open()?;
    let rows = client.query_places(&cfg, args.confidence)?;
    write_json(&args.base.output, &rows)
}

fn cfg_from_extract_args(args: &ExtractArgs) -> Result<OvertureConfig> {
    Ok(OvertureConfig {
        release: args.overture_release.clone(),
        bbox: bbox_from_values(args.min_lon, args.min_lat, args.max_lon, args.max_lat)?,
    })
}

fn bbox_from_values(min_lon: f64, min_lat: f64, max_lon: f64, max_lat: f64) -> Result<Bbox> {
    if min_lon >= max_lon || min_lat >= max_lat {
        bail!("invalid bbox: min values must be smaller than max values");
    }
    Ok(Bbox {
        min_lon,
        min_lat,
        max_lon,
        max_lat,
    })
}

fn write_json<T: serde::Serialize>(path: &PathBuf, value: &T) -> Result<()> {
    let file = File::create(path).with_context(|| format!("creating {}", path.display()))?;
    let writer = BufWriter::new(file);
    serde_json::to_writer_pretty(writer, value)
        .with_context(|| format!("writing {}", path.display()))
}
