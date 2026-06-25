use serde::{Deserialize, Serialize};

const EARTH_RADIUS_M: f64 = 6_378_137.0;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
pub struct LonLat {
    pub lon: f64,
    pub lat: f64,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
pub struct Point {
    pub x: f64,
    pub y: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub z: Option<f64>,
}

#[derive(Debug, Clone, Copy)]
pub struct Projector {
    lon0: f64,
    lat0: f64,
}

impl Projector {
    pub fn from_proj_str(proj: &str, fallback_center: LonLat) -> Self {
        let lon0 = parse_proj_value(proj, "lon_0").unwrap_or(fallback_center.lon);
        let lat0 = parse_proj_value(proj, "lat_0").unwrap_or(fallback_center.lat);
        Self { lon0, lat0 }
    }

    pub fn project(&self, p: LonLat) -> Point {
        let lon_delta = (p.lon - self.lon0).to_radians();
        let lat_delta = (p.lat - self.lat0).to_radians();
        let lat_scale = self.lat0.to_radians().cos();
        Point {
            x: EARTH_RADIUS_M * lon_delta * lat_scale,
            y: EARTH_RADIUS_M * lat_delta,
            z: None,
        }
    }
}

fn parse_proj_value(proj: &str, key: &str) -> Option<f64> {
    let prefix = format!("+{}=", key);
    proj.split_whitespace()
        .find_map(|part| part.strip_prefix(&prefix))
        .and_then(|value| value.parse::<f64>().ok())
}

pub fn line_length(points: &[Point]) -> f64 {
    points
        .windows(2)
        .map(|w| {
            let dx = w[1].x - w[0].x;
            let dy = w[1].y - w[0].y;
            dx.hypot(dy)
        })
        .sum()
}

pub fn polygon_area(points: &[Point]) -> f64 {
    if points.len() < 3 {
        return 0.0;
    }
    let signed: f64 = points
        .iter()
        .zip(points.iter().cycle().skip(1))
        .take(points.len())
        .map(|(a, b)| a.x * b.y - b.x * a.y)
        .sum();
    0.5 * signed.abs()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_proj_origin() {
        let p = Projector::from_proj_str(
            "+proj=tmerc +lat_0=30.0 +lon_0=120.0",
            LonLat { lon: 0.0, lat: 0.0 },
        );
        let xy = p.project(LonLat {
            lon: 120.0,
            lat: 30.0,
        });
        assert!(xy.x.abs() < 1e-9);
        assert!(xy.y.abs() < 1e-9);
    }

    #[test]
    fn computes_length_and_area() {
        let line = vec![
            Point {
                x: 0.0,
                y: 0.0,
                z: None,
            },
            Point {
                x: 3.0,
                y: 4.0,
                z: None,
            },
        ];
        assert_eq!(line_length(&line), 5.0);
        let poly = vec![
            Point {
                x: 0.0,
                y: 0.0,
                z: None,
            },
            Point {
                x: 2.0,
                y: 0.0,
                z: None,
            },
            Point {
                x: 2.0,
                y: 2.0,
                z: None,
            },
            Point {
                x: 0.0,
                y: 2.0,
                z: None,
            },
        ];
        assert_eq!(polygon_area(&poly), 4.0);
    }
}
