use std::collections::HashMap;

use chrono::Local;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};

use crate::overture::{Bbox, Building, Place, RoadSegment};
use crate::projection::{line_length, polygon_area, LonLat, Point, Projector};

const LANE_START_ID: i64 = 0;
const ROAD_START_ID: i64 = 200_000_000;
const JUNCTION_START_ID: i64 = 300_000_000;
const AOI_START_ID: i64 = 500_000_000;
const POI_START_ID: i64 = 700_000_000;

const LANE_TYPE_DRIVING: i32 = 1;
const LANE_TURN_STRAIGHT: i32 = 1;
const LANE_CONNECTION_HEAD: i32 = 1;
const LANE_CONNECTION_TAIL: i32 = 2;
const DEFAULT_LANE_WIDTH: f64 = 3.2;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct MapOutput {
    pub header: Header,
    pub lanes: Vec<Lane>,
    pub roads: Vec<Road>,
    pub junctions: Vec<Junction>,
    pub aois: Vec<Aoi>,
    pub pois: Vec<Poi>,
    #[serde(rename = "_sublines")]
    pub sublines: Vec<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Header {
    pub name: String,
    pub date: String,
    pub north: f64,
    pub south: f64,
    pub west: f64,
    pub east: f64,
    pub projection: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Lane {
    pub id: i64,
    #[serde(rename = "type")]
    pub lane_type: i32,
    pub turn: i32,
    pub max_speed: f64,
    pub length: f64,
    pub width: f64,
    pub center_line: Line,
    pub predecessors: Vec<LaneConnection>,
    pub successors: Vec<LaneConnection>,
    pub left_lane_ids: Vec<i64>,
    pub right_lane_ids: Vec<i64>,
    pub parent_id: i64,
    pub overlaps: Vec<serde_json::Value>,
    pub aoi_ids: Vec<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Line {
    pub nodes: Vec<Point>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct LaneConnection {
    pub id: i64,
    #[serde(rename = "type")]
    pub connection_type: i32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Road {
    pub id: i64,
    pub lane_ids: Vec<i64>,
    pub name: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Junction {
    pub id: i64,
    pub lane_ids: Vec<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Aoi {
    pub id: i64,
    pub positions: Vec<Point>,
    pub area: f64,
    pub driving_positions: Vec<LanePosition>,
    pub driving_gates: Vec<Point>,
    pub walking_positions: Vec<LanePosition>,
    pub walking_gates: Vec<Point>,
    pub poi_ids: Vec<i64>,
    pub name: String,
    pub urban_land_use: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct LanePosition {
    pub lane_id: i64,
    pub s: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Poi {
    pub id: i64,
    pub name: String,
    pub category: String,
    pub position: Point,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub aoi_id: Option<i64>,
}

#[derive(Debug, Clone)]
pub struct BuildInput {
    pub name: String,
    pub projection: String,
    pub bbox: Bbox,
    pub roads: Vec<RoadSegment>,
    pub buildings: Vec<Building>,
    pub places: Vec<Place>,
}

pub fn build_map(input: BuildInput) -> MapOutput {
    let projector = Projector::from_proj_str(&input.projection, input.bbox.center());

    let mut lanes: Vec<Lane> = input
        .roads
        .par_iter()
        .enumerate()
        .filter_map(|(idx, segment)| lane_from_segment(idx, segment, projector))
        .collect();
    lanes.par_sort_unstable_by_key(|lane| lane.id);

    let mut roads: Vec<Road> = input
        .roads
        .par_iter()
        .enumerate()
        .map(|(idx, segment)| {
            let id = ROAD_START_ID + idx as i64;
            Road {
                id,
                lane_ids: vec![LANE_START_ID + idx as i64],
                name: segment.name.clone(),
            }
        })
        .collect();
    roads.par_sort_unstable_by_key(|road| road.id);

    add_lane_connections(&mut lanes);
    let junctions = infer_junctions(&lanes);

    let mut aois: Vec<Aoi> = input
        .buildings
        .par_iter()
        .enumerate()
        .filter_map(|(idx, building)| aoi_from_building(idx, building, projector))
        .collect();
    aois.par_sort_unstable_by_key(|aoi| aoi.id);

    let mut pois: Vec<Poi> = input
        .places
        .par_iter()
        .enumerate()
        .map(|(idx, place)| poi_from_place(idx, place, projector))
        .collect();
    pois.par_sort_unstable_by_key(|poi| poi.id);

    let header = header_from_lanes(&input.name, &input.projection, &lanes);
    MapOutput {
        header,
        lanes,
        roads,
        junctions,
        aois,
        pois,
        sublines: Vec::new(),
    }
}

fn lane_from_segment(idx: usize, segment: &RoadSegment, projector: Projector) -> Option<Lane> {
    if segment.geometry.len() < 2 {
        return None;
    }
    let nodes: Vec<Point> = segment
        .geometry
        .iter()
        .map(|p| projector.project(*p))
        .collect();
    let id = LANE_START_ID + idx as i64;
    let parent_id = ROAD_START_ID + idx as i64;
    Some(Lane {
        id,
        lane_type: LANE_TYPE_DRIVING,
        turn: LANE_TURN_STRAIGHT,
        max_speed: class_speed_mps(&segment.class),
        length: line_length(&nodes),
        width: DEFAULT_LANE_WIDTH,
        center_line: Line { nodes },
        predecessors: Vec::new(),
        successors: Vec::new(),
        left_lane_ids: Vec::new(),
        right_lane_ids: Vec::new(),
        parent_id,
        overlaps: Vec::new(),
        aoi_ids: Vec::new(),
    })
}

fn add_lane_connections(lanes: &mut [Lane]) {
    let mut starts: HashMap<EndpointKey, Vec<i64>> = HashMap::new();
    let mut ends: HashMap<EndpointKey, Vec<i64>> = HashMap::new();
    for lane in lanes.iter() {
        if let (Some(start), Some(end)) = (
            lane.center_line.nodes.first(),
            lane.center_line.nodes.last(),
        ) {
            starts
                .entry(EndpointKey::from_point(start))
                .or_default()
                .push(lane.id);
            ends.entry(EndpointKey::from_point(end))
                .or_default()
                .push(lane.id);
        }
    }

    let mut predecessors: HashMap<i64, Vec<LaneConnection>> = HashMap::new();
    let mut successors: HashMap<i64, Vec<LaneConnection>> = HashMap::new();
    for (key, ending_lanes) in ends {
        if let Some(starting_lanes) = starts.get(&key) {
            for from in &ending_lanes {
                for to in starting_lanes {
                    if from == to {
                        continue;
                    }
                    successors.entry(*from).or_default().push(LaneConnection {
                        id: *to,
                        connection_type: LANE_CONNECTION_HEAD,
                    });
                    predecessors.entry(*to).or_default().push(LaneConnection {
                        id: *from,
                        connection_type: LANE_CONNECTION_TAIL,
                    });
                }
            }
        }
    }

    for lane in lanes {
        lane.predecessors = predecessors.remove(&lane.id).unwrap_or_default();
        lane.successors = successors.remove(&lane.id).unwrap_or_default();
        lane.predecessors.sort_by_key(|conn| conn.id);
        lane.successors.sort_by_key(|conn| conn.id);
    }
}

fn infer_junctions(lanes: &[Lane]) -> Vec<Junction> {
    let mut endpoints: HashMap<EndpointKey, Vec<i64>> = HashMap::new();
    for lane in lanes {
        if let Some(start) = lane.center_line.nodes.first() {
            endpoints
                .entry(EndpointKey::from_point(start))
                .or_default()
                .push(lane.id);
        }
        if let Some(end) = lane.center_line.nodes.last() {
            endpoints
                .entry(EndpointKey::from_point(end))
                .or_default()
                .push(lane.id);
        }
    }
    let mut junctions: Vec<Junction> = endpoints
        .into_values()
        .filter(|lane_ids| lane_ids.len() > 1)
        .enumerate()
        .map(|(idx, mut lane_ids)| {
            lane_ids.sort_unstable();
            lane_ids.dedup();
            Junction {
                id: JUNCTION_START_ID + idx as i64,
                lane_ids,
            }
        })
        .collect();
    junctions.sort_by_key(|junction| junction.id);
    junctions
}

fn aoi_from_building(idx: usize, building: &Building, projector: Projector) -> Option<Aoi> {
    let exterior = building.rings.first()?;
    if exterior.len() < 3 {
        return None;
    }
    let positions: Vec<Point> = exterior.iter().map(|p| projector.project(*p)).collect();
    Some(Aoi {
        id: AOI_START_ID + idx as i64,
        area: polygon_area(&positions),
        positions,
        driving_positions: Vec::new(),
        driving_gates: Vec::new(),
        walking_positions: Vec::new(),
        walking_gates: Vec::new(),
        poi_ids: Vec::new(),
        name: building.name.clone(),
        urban_land_use: if building.class.is_empty() {
            building.subtype.clone()
        } else {
            building.class.clone()
        },
    })
}

fn poi_from_place(idx: usize, place: &Place, projector: Projector) -> Poi {
    Poi {
        id: POI_START_ID + idx as i64,
        name: place.name.clone(),
        category: place.category.clone(),
        position: projector.project(place.position),
        aoi_id: None,
    }
}

fn header_from_lanes(name: &str, projection: &str, lanes: &[Lane]) -> Header {
    let mut west = f64::INFINITY;
    let mut east = f64::NEG_INFINITY;
    let mut south = f64::INFINITY;
    let mut north = f64::NEG_INFINITY;
    for node in lanes.iter().flat_map(|lane| lane.center_line.nodes.iter()) {
        west = west.min(node.x);
        east = east.max(node.x);
        south = south.min(node.y);
        north = north.max(node.y);
    }
    if lanes.is_empty() {
        west = 0.0;
        east = 0.0;
        south = 0.0;
        north = 0.0;
    }
    Header {
        name: name.to_string(),
        date: Local::now().format("%a %b %d %H:%M:%S %Y").to_string(),
        north,
        south,
        west,
        east,
        projection: projection.to_string(),
    }
}

fn class_speed_mps(class: &str) -> f64 {
    let kmh = match class {
        "motorway" => 120.0,
        "trunk" => 90.0,
        "primary" => 60.0,
        "secondary" => 50.0,
        "tertiary" => 40.0,
        "residential" => 30.0,
        "service" => 20.0,
        _ => 40.0,
    };
    kmh / 3.6
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
struct EndpointKey {
    x_mm: i64,
    y_mm: i64,
}

impl EndpointKey {
    fn from_point(point: &Point) -> Self {
        Self {
            x_mm: (point.x * 1000.0).round() as i64,
            y_mm: (point.y * 1000.0).round() as i64,
        }
    }
}

#[allow(dead_code)]
fn _bbox_center(bbox: Bbox) -> LonLat {
    bbox.center()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_deterministic_map_from_fixtures() {
        let input = BuildInput {
            name: "fixture".to_string(),
            projection: "+proj=tmerc +lat_0=0 +lon_0=0".to_string(),
            bbox: Bbox {
                min_lon: 0.0,
                min_lat: 0.0,
                max_lon: 1.0,
                max_lat: 1.0,
            },
            roads: vec![
                RoadSegment {
                    id: "b".to_string(),
                    name: "B".to_string(),
                    class: "primary".to_string(),
                    geometry: vec![
                        LonLat { lon: 0.0, lat: 0.0 },
                        LonLat {
                            lon: 0.001,
                            lat: 0.0,
                        },
                    ],
                },
                RoadSegment {
                    id: "a".to_string(),
                    name: "A".to_string(),
                    class: "secondary".to_string(),
                    geometry: vec![
                        LonLat {
                            lon: 0.001,
                            lat: 0.0,
                        },
                        LonLat {
                            lon: 0.002,
                            lat: 0.0,
                        },
                    ],
                },
            ],
            buildings: vec![Building {
                id: "building".to_string(),
                name: "Building".to_string(),
                subtype: "residential".to_string(),
                class: String::new(),
                rings: vec![vec![
                    LonLat { lon: 0.0, lat: 0.0 },
                    LonLat {
                        lon: 0.001,
                        lat: 0.0,
                    },
                    LonLat {
                        lon: 0.001,
                        lat: 0.001,
                    },
                    LonLat { lon: 0.0, lat: 0.0 },
                ]],
            }],
            places: vec![Place {
                id: "place".to_string(),
                name: "Place".to_string(),
                category: "cafe".to_string(),
                confidence: Some(0.9),
                position: LonLat {
                    lon: 0.0005,
                    lat: 0.0,
                },
            }],
        };

        let output = build_map(input);
        assert_eq!(output.lanes.len(), 2);
        assert_eq!(output.roads.len(), 2);
        assert_eq!(output.junctions.len(), 1);
        assert_eq!(output.aois.len(), 1);
        assert_eq!(output.pois.len(), 1);
        assert_eq!(output.lanes[0].successors[0].id, output.lanes[1].id);
    }
}
