import logging
from typing import Optional

import requests
from geojson import Feature, FeatureCollection, Point, dump
from ...spatial_db.spatial_db import SpatialDB
import pandas as pd

from .._map_util.format_checker import osm_format_checker

__all__ = ["PointOfInterest"]


class PointOfInterest:
    """
    Process OSM raw data to POI as geojson format files
    """

    def __init__(
        self,
        max_longitude: Optional[float] = None,
        min_longitude: Optional[float] = None,
        max_latitude: Optional[float] = None,
        min_latitude: Optional[float] = None,
        wikipedia_name: Optional[str] = None,
        proxies: Optional[dict[str, str]] = None,
    ):
        """
        Args:
        - max_longitude (Optional[float]): max longitude
        - min_longitude (Optional[float]): min longitude
        - max_latitude (Optional[float]): max latitude
        - min_latitude (Optional[float]): min latitude
        - wikipedia_name (Optional[str]): wikipedia name of the area in OSM.
        - proxies (Optional[dict[str, str]]): proxies for requests, e.g. {'http': 'http://localhost:1080', 'https': 'http://localhost:1080'}
        """
        self.bbox = (
            min_latitude,
            min_longitude,
            max_latitude,
            max_longitude,
        )
        self.wikipedia_name = wikipedia_name
        self.proxies = proxies
        # OSM raw data
        self._nodes = []

        # generate POIs
        self.pois: list = []
        self.sp_db = SpatialDB()

    def _query_raw_data(self, osm_data_cache: Optional[list[dict]] = None):
        """
        Get raw data from OSM API
        OSM query language: https://wiki.openstreetmap.org/wiki/Overpass_API/Language_Guide
        Can be run and visualized in real time at https://overpass-turbo.eu/
        """
        if osm_data_cache is None:
            logging.info("Querying osm raw data")
            assert all(
                i is not None for i in self.bbox
            ), f"longitude and latitude are required without cache file!"
            bbox_str = ",".join(str(i) for i in self.bbox)
            query_header = f"[out:json][timeout:120][bbox:{bbox_str}];"
            area_wikipedia_name = self.wikipedia_name
            if area_wikipedia_name is not None:
                query_header += f'area[wikipedia="{area_wikipedia_name}"]->.searchArea;'
            osm_data = None
            for _ in range(3):  # retry 3 times
                try:
                    query_body_raw = [
                        (
                            "node",
                            "",
                        ),
                    ]
                    query_body = ""
                    for obj, args in query_body_raw:
                        area = (
                            "(area.searchArea)"
                            if area_wikipedia_name is not None
                            else ""
                        )
                        query_body += obj + area + args + ";"
                    query_body = "(" + query_body + ");"
                    query = query_header + query_body + "(._;>;);" + "out body;"
                    logging.info(f"{query}")
                    osm_data = requests.get(
                        "http://overpass-api.de/api/interpreter?data=" + query,
                        proxies=self.proxies,
                    ).json()["elements"]
                    break
                except Exception as e:
                    logging.warning(f"Exception when querying OSM data {e}")
                    logging.warning("No response from OSM, Please try again later!")
            if osm_data is None:
                raise Exception("No POI response from OSM!")
        else:
            osm_data = osm_data_cache
        nodes = [d for d in osm_data if d["type"] == "node"]
        logging.info(f"node: {len(nodes)}")
        self._nodes = nodes

    def _make_raw_poi(self):
        """
        Construct POI from original OSM data.
        """
        _raw_pois = []
        for d in self._nodes:
            d_tags = d.get("tags", {})
            p_name = d_tags.get("name", "")
            p_address = d_tags.get("address", "")

            # name
            d["name"] = p_name
            d["address"] = p_address
            # catg
            if "landuse" in d_tags:
                value = d_tags["landuse"]
                # Exclude invalid fields
                if not "yes" in value:
                    p_catg = value
                    d["catg"] = p_catg
                    _raw_pois.append(d)
                    continue
            if "leisure" in d_tags:
                value = d_tags["leisure"]
                if not "yes" in value:
                    p_catg = "leisure|" + value
                    d["catg"] = p_catg
                    _raw_pois.append(d)
                    continue
            if "amenity" in d_tags:
                value = d_tags["amenity"]
                if not "yes" in value:
                    p_catg = "amenity|" + value
                    d["catg"] = p_catg
                    _raw_pois.append(d)
                    continue
            if "building" in d_tags:
                value = d_tags["building"]
                if not "yes" in value:
                    p_catg = "building|" + value
                    d["catg"] = p_catg
                    _raw_pois.append(d)
                    continue
        logging.info(f"raw poi: {len(_raw_pois)}")
        self.pois = _raw_pois

    def _query_pois_from_overture(self, confidence_filter: float = 0.8):
        """
        Query POIs from Overture Maps API.

        Args:
        - confidence_filter (float): confidence filter for POIs.

        Returns:
        - List of POI dictionaries
        """
        logging.info(
            f"Querying POIs from Overture Maps in bbox: {self.bbox} with confidence filter: {confidence_filter}"
        )

        try:
            result = self.sp_db.execute(
                f"""
              LOAD spatial;
              LOAD httpfs;

              -- Access the data on AWS
              SET s3_region='us-west-2';

              SELECT 
                id,
                names.primary as name,
                basic_category as catg,
                ST_X(geometry) as lon,
                ST_Y(geometry) as lat,
                COALESCE(addresses[1].freeform, '') as address,
                confidence,
                sources
              FROM read_parquet('s3://overturemaps-us-west-2/release/2026-01-21.0/theme=places/type=place/*')
              WHERE 
                  confidence >= {confidence_filter}
                  -- Filter by actual point geometry, not bbox
                  AND ST_X(geometry) BETWEEN {self.bbox[1]} AND {self.bbox[3]}
                  AND ST_Y(geometry) BETWEEN {self.bbox[0]} AND {self.bbox[2]}
              """
            ).fetchall()

            logging.info(f"Found {len(result)} POIs from Overture Maps")

            # Convert to the expected format
            pois = []
            for row in result:
                pois.append(
                    {
                        "id": row[0],
                        "name": row[1] or "",
                        "catg": row[2] or "unknown",
                        "lon": row[3],
                        "lat": row[4],
                        "address": row[5] or "",
                        "tags": {"confidence": row[6]},
                    }
                )

            return pois

        except Exception as e:
            logging.error(f"Error querying Overture Maps: {e}")
            raise

    def _merge_overture_and_osm_pois(
        self,
        overture_pois: list[dict],
        osm_pois: list[dict],
    ) -> list[dict]:
        """
        Merge POIs from Overture Maps and OSM data.

        Args:
        - overture_pois (list[dict]): List of POIs from Overture Maps.
        - osm_pois (list[dict]): List of POIs from OSM data.

        Returns:
        - Merged list of POIs.
        """
        logging.info(f"\n\n\n\nOverture POIs: {overture_pois[:5]}")
        logging.info(f"\n\n\n\nOSM POIs: {osm_pois[:5]}")

        flattened_overture_pois = [
            {
                "id": p["id"],
                "name": p["name"],
                "catg": p["catg"],
                "lon": p["lon"],
                "lat": p["lat"],
                "address": p.get("address", ""),
            }
            for p in overture_pois
        ]

        # Convert to pandas DataFrame and register with DuckDB
        overture_df = pd.DataFrame(flattened_overture_pois)

        if overture_df.empty:
            logging.warning("No Overture POIs to merge, returning OSM POIs only.")
            return osm_pois

        # Register the DataFrame with DuckDB
        self.sp_db.con.register("overture_df", overture_df)

        self.sp_db.execute(
            """
            CREATE TABLE overture_pois AS 
            SELECT *, h3_latlng_to_cell(lat, lon, 10) AS h3_index 
            FROM overture_df
        """
        )

        flattened_osm_pois = [
            {
                "id": p["id"],
                "name": p["name"],
                "catg": p["catg"],
                "lon": p["lon"],
                "lat": p["lat"],
                "address": p.get("address", ""),
            }
            for p in osm_pois
        ]

        # Convert to pandas DataFrame and register with DuckDB
        osm_df = pd.DataFrame(flattened_osm_pois)

        if osm_df.empty:
            logging.warning("No OSM POIs to merge, returning Overture POIs only.")
            return overture_pois

        # Register the DataFrame with DuckDB
        self.sp_db.con.register("osm_df", osm_df)

        self.sp_db.execute(
            """
            CREATE TABLE osm_pois AS 
            SELECT *, h3_latlng_to_cell(lat, lon, 10) AS h3_index 
            FROM osm_df
        """
        )

        similar_samples = self.sp_db.execute(
            """
        SELECT o.id, o.name, o.catg, osm.id, osm.name, osm.catg,
              jaro_winkler_similarity(LOWER(TRIM(o.name)), LOWER(TRIM(osm.name))) as similarity
        FROM overture_pois o
        JOIN osm_pois osm
        ON o.h3_index = osm.h3_index
        WHERE jaro_winkler_similarity(LOWER(TRIM(o.name)), LOWER(TRIM(osm.name))) >= 0.9
        ORDER BY similarity DESC
        LIMIT 5
        """
        ).fetchall()

        count_similar_names = self.sp_db.execute(
            """
        SELECT COUNT(*) AS count
        FROM overture_pois o
        JOIN osm_pois osm
        ON o.h3_index = osm.h3_index
        WHERE jaro_winkler_similarity(LOWER(TRIM(o.name)), LOWER(TRIM(osm.name))) >= 0.9
        """
        ).fetchall()

        logging.info(
            f"Found {count_similar_names[0][0]} similar POIs between Overture and OSM. Sample: {similar_samples[:2]}"
        )

        # Remove exact name matches based on h3 index and name
        # Preprocess names to lower case and trim spaces
        self.sp_db.execute(
            """
        DELETE FROM osm_pois
        WHERE EXISTS (
            SELECT 1
            FROM overture_pois o
            WHERE osm_pois.h3_index = o.h3_index
            AND jaro_winkler_similarity(LOWER(TRIM(o.name)), LOWER(TRIM(osm_pois.name))) >= 0.9
        )             
        """
        )

        similar_samples_based_address = self.sp_db.execute(
            """
        SELECT o.id, o.name, o.catg, osm.id, osm.name, osm.catg,
              jaro_winkler_similarity(LOWER(TRIM(o.name)), LOWER(TRIM(osm.name))) as similarity,
              jaro_winkler_similarity(LOWER(TRIM(o.address)), LOWER(TRIM(osm.address))) as address_similarity
        FROM overture_pois o
        JOIN osm_pois osm
        ON o.h3_index = osm.h3_index
        WHERE jaro_winkler_similarity(LOWER(TRIM(o.name)), LOWER(TRIM(osm.name))) >= 0.75 AND o.address != '' AND osm.address != '' AND jaro_winkler_similarity(LOWER(TRIM(o.address)), LOWER(TRIM(osm.address))) >= 0.75
        ORDER BY similarity DESC
        LIMIT 5
        """
        ).fetchall()

        count_similar_samples_based_address = self.sp_db.execute(
            """
        SELECT COUNT(*) AS count
        FROM overture_pois o
        JOIN osm_pois osm
        ON o.h3_index = osm.h3_index
        WHERE jaro_winkler_similarity(LOWER(TRIM(o.name)), LOWER(TRIM(osm.name))) >= 0.75 AND o.address != '' AND osm.address != '' AND jaro_winkler_similarity(LOWER(TRIM(o.address)), LOWER(TRIM(osm.address))) >= 0.75
        """
        ).fetchall()

        logging.info(
            f"Found {count_similar_samples_based_address[0][0]} similar POIs between Overture and OSM (using name + address). Sample: {similar_samples_based_address[:2]}"
        )

        self.sp_db.execute(
            """
        DELETE FROM osm_pois
        WHERE EXISTS (
            SELECT 1
            FROM overture_pois o
            WHERE osm_pois.h3_index = o.h3_index
            AND jaro_winkler_similarity(LOWER(TRIM(o.name)), LOWER(TRIM(osm_pois.name))) >= 0.75
            AND o.address != '' AND osm_pois.address != '' AND jaro_winkler_similarity(LOWER(TRIM(o.address)), LOWER(TRIM(osm_pois.address))) >= 0.75
        )             
        """
        )

        final_pois = self.sp_db.execute(
            """
        SELECT id, name, catg, lon, lat  FROM overture_pois
        UNION ALL
        SELECT id, name, catg, lon, lat FROM osm_pois
        """
        ).fetchall()

        # Convert back to list of dicts
        merged_pois = []
        for row in final_pois:
            merged_pois.append(
                {
                    "id": row[0],
                    "name": row[1],
                    "catg": row[2],
                    "lon": row[3],
                    "lat": row[4],
                    "tags": {},
                }
            )

        logging.info(f"Total merged POIs: {len(merged_pois)}")
        return merged_pois

    def create_pois(
        self,
        output_path: Optional[str] = None,
        osm_data_cache: Optional[list[dict]] = None,
        osm_cache_check: bool = False,
        use_overture_maps: bool = False,
        merge_data: bool = False,
        confidence_filter: float = 0.8,
    ):
        """
        Create POIs from OpenStreetMap or Overture Maps.

        Args:
        - osm_data_cache (Optional[list[dict]]): OSM data cache.
        - output_path (str): GeoJSON file output path.
        - osm_cache_check (bool): check the format of input OSM data cache.
        - use_overture_maps (bool): whether to use Overture Maps for POIs.
        - confidence_filter (float): confidence filter for POIs.
        Returns:
        - POIs in GeoJSON format.
        """
        if use_overture_maps:
            overture_pois = self._query_pois_from_overture(
                confidence_filter=confidence_filter
            )
            logging.info(f"Overture POIs: {len(overture_pois)}")
            self.pois = overture_pois

        if (not use_overture_maps) or merge_data:
            logging.info(f"Getting POIs from OSM data")
            osm_format_checker(
                osm_cache_check, osm_data_cache, {"node": ["lon", "lat"]}
            )
            self._query_raw_data(osm_data_cache)
            self._make_raw_poi()

        if merge_data:
            self.pois = self._merge_overture_and_osm_pois(
                overture_pois=overture_pois, osm_pois=self.pois
            )

        geos = []
        logging.info("Generating POI geojson")
        logging.info(f"\npoi: {len(self.pois)}")
        for poi_id, poi in enumerate(self.pois):
            geos.append(
                Feature(
                    geometry=Point([poi["lon"], poi["lat"]]),
                    properties={
                        "id": poi_id,
                        "osm_tags": poi["tags"],
                        "name": poi["name"],
                        "catg": poi["catg"],
                    },
                )
            )
        geos = FeatureCollection(geos)
        if output_path is not None:
            with open(output_path, encoding="utf-8", mode="w") as f:
                dump(geos, f, indent=2, ensure_ascii=False)
        return geos
