import logging
import duckdb
import pandas as pd
import tempfile
import atexit
import os
from shapely import wkt as shapely_wkt
from tqdm import tqdm
from shapely.geometry import Polygon, MultiPolygon


class SpatialDB:
    def __init__(self, memory_limit="64GB"):
        # Create a temp file path but remove the empty file immediately
        self.fd, self.db_path = tempfile.mkstemp(suffix=".duckdb")
        os.close(self.fd)  # Close the file descriptor
        os.remove(self.db_path)  # Remove the empty file

        # Now DuckDB can create a new database at this path
        self.con = duckdb.connect(self.db_path)
        self.con.execute(f"PRAGMA memory_limit='{memory_limit}'")

        self.con.sql("INSTALL httpfs;")
        self.con.sql("INSTALL spatial;")
        self.con.sql("INSTALL h3 FROM community;")
        self.con.sql("LOAD httpfs;")
        self.con.sql("LOAD spatial;")
        self.con.sql("LOAD h3")

        atexit.register(self.cleanup)

    def cleanup(self):
        try:
            self.con.close()
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
        except Exception as e:
            pass

    def execute(self, query: str):
        return self.con.sql(query)

    def _ingest_pois_and_aois(
        self, pois: list[dict], aois: list[dict], simplify_tolerance: float = 1.0
    ):
        """
        Ingest the pois and aois into the spatial database for efficient spatial queries.
        This is used for matching the pois to the aois.

        Args:
            simplify_tolerance: Tolerance for simplifying AOI geometries (meters).
                            Higher values = more simplification = less memory.
        """

        # Extract only the data we need (exclude Shapely geometry objects)
        pois_clean = []
        for poi in pois:
            pois_clean.append(
                {
                    "id": poi["id"],
                    "name": poi.get("name", ""),
                    "category": poi.get("category", ""),
                    "lon": poi["coords"][0][0],
                    "lat": poi["coords"][0][1],
                    "external": poi.get("external", {}),
                }
            )

        aois_clean = []
        for aoi in aois:
            # Extract coordinates as a list of tuples
            coords = aoi["coords"]
            aois_clean.append(
                {
                    "id": aoi["id"],
                    "coords": coords,
                    "external": aoi.get("external", {}),
                }
            )

        # Convert to pandas DataFrames
        pois_df = pd.DataFrame(pois_clean)
        aois_df = pd.DataFrame(aois_clean)

        # Register DataFrames with DuckDB
        self.con.register("pois_df", pois_df)
        self.con.register("aois_df", aois_df)

        # Create POI table with geometries
        self.con.execute(
            """
        CREATE TABLE pois AS
        SELECT
            id,
            name,
            category,
            external,
            ST_Point(lon, lat) AS geom
        FROM pois_df;
        """
        )

        # Create AOI table with simplified polygon geometries
        # We need to construct WKT POLYGON strings from the coords
        aois_with_wkt = []
        for aoi in aois:
            # Use the existing shapely object if available, or its WKT string
            wkt = (
                aoi["geo"].wkt
                if hasattr(aoi["geo"], "wkt")
                else f"POLYGON(({', '.join([f'{x} {y}' for x, y in aoi['coords']])}))"
            )
            aois_with_wkt.append(
                {"id": aoi["id"], "wkt": wkt, "external": aoi.get("external", {})}
            )

        aois_wkt_df = pd.DataFrame(aois_with_wkt)
        self.con.register("aois_wkt_df", aois_wkt_df)

        # Simplify geometries to reduce memory usage
        self.con.execute(
            f"""
        CREATE TABLE aois AS
        SELECT
            id,
            external,
            ST_Simplify(ST_GeomFromText(wkt), {simplify_tolerance}) AS geom
        FROM aois_wkt_df;
        """
        )

        # Create spatial indices
        self.con.execute("CREATE INDEX idx_pois_geom ON pois USING RTREE(geom);")
        self.con.execute("CREATE INDEX idx_aois_geom ON aois USING RTREE(geom);")

        logging.info("Ingested POIs and AOIs into spatial database")
        logging.info(
            f"POI count: {self.con.execute('SELECT COUNT(*) FROM pois;').fetchone()[0]}"
        )
        logging.info(
            f"AOI count: {self.con.execute('SELECT COUNT(*) FROM aois;').fetchone()[0]}"
        )

    def match_pois_to_aois(
        self,
        pois: list[dict],
        aois: list[dict],
        distance_threshold: float = 18.0,
        simplify_tolerance: float = 1.0,
        batch_size: int = 10000,
    ):
        """
        Matches POIs to AOIs using DuckDB spatial logic with batching.
        Replaces the iterative _match_poi_unit function.

        Args:
            pois: List of POI dictionaries
            aois: List of AOI dictionaries
            distance_threshold: Maximum distance for neighboring POIs
            simplify_tolerance: Tolerance for simplifying AOI geometries
            batch_size: Number of POIs to process per batch

        Returns:
            List of tuples:
            - (poi_dict, aoi_index, 0) for covered POIs
            - (poi_dict, aoi_index, 1) for neighboring/projected POIs
            - (poi_dict, 2) for isolated POIs
        """
        # Set memory-efficient settings
        self.con.execute("SET preserve_insertion_order=false;")

        self._ingest_pois_and_aois(pois, aois, simplify_tolerance=simplify_tolerance)

        poi_dict = {poi["id"]: poi for poi in pois}
        aoi_id_to_index = {aoi["id"]: idx for idx, aoi in enumerate(aois)}

        all_results = []

        # Process in batches to reduce memory usage
        total_pois = self.con.execute("SELECT COUNT(*) FROM pois").fetchone()[0]
        logging.info(f"Processing {total_pois} POIs in batches of {batch_size}")

        for offset in tqdm(
            range(0, total_pois, batch_size), desc="Matching POIs to AOIs"
        ):
            logging.info(
                f"Processing POI batch {offset} to {min(offset + batch_size, total_pois)}"
            )

            # Step 1: Get covered POIs in this batch
            covered_pois_result = self.execute(
                f"""
                WITH batch_pois AS (
                    SELECT id, geom 
                    FROM pois 
                    ORDER BY id
                    LIMIT {batch_size} OFFSET {offset}
                ),
                poi_aoi_coverage AS (
                    SELECT p.id as poi_id, a.id AS aoi_id
                    FROM batch_pois p
                    JOIN aois a ON ST_Within(p.geom, a.geom)
                ),
                ranked_coverage AS (
                    SELECT 
                        poi_id, 
                        aoi_id,
                        ROW_NUMBER() OVER (PARTITION BY poi_id ORDER BY aoi_id) as rn
                    FROM poi_aoi_coverage
                )
                SELECT poi_id, aoi_id
                FROM ranked_coverage
                WHERE rn = 1
                """
            ).fetchall()

            covered_poi_ids = {poi_id for poi_id, _ in covered_pois_result}

            # Step 2: Get neighboring POIs in this batch
            if covered_poi_ids:
                covered_ids_str = ",".join(map(str, covered_poi_ids))
                exclude_covered = f"AND p.id NOT IN ({covered_ids_str})"
            else:
                exclude_covered = ""

            neighboring_pois_result = self.execute(
                f"""
                WITH batch_pois AS (
                    SELECT id, geom 
                    FROM pois 
                    ORDER BY id
                    LIMIT {batch_size} OFFSET {offset}
                ),
                poi_aoi_neighbors AS (
                    SELECT p.id as poi_id, a.id AS aoi_id, ST_Distance(p.geom, a.geom) as distance
                    FROM batch_pois p
                    JOIN aois a ON ST_DWithin(p.geom, a.geom, {distance_threshold})
                    WHERE NOT ST_Within(p.geom, a.geom)
                    {exclude_covered}
                ),
                ranked_neighbors AS (
                    SELECT 
                        poi_id, 
                        aoi_id, 
                        distance,
                        ROW_NUMBER() OVER (PARTITION BY poi_id ORDER BY distance) as rn
                    FROM poi_aoi_neighbors
                )
                SELECT poi_id, aoi_id, distance
                FROM ranked_neighbors
                WHERE rn = 1
                """
            ).fetchall()

            neighboring_poi_ids = {poi_id for poi_id, _, _ in neighboring_pois_result}

            # Get batch POI IDs
            batch_poi_ids = set(
                self.execute(
                    f"""
                SELECT id FROM pois 
                ORDER BY id
                LIMIT {batch_size} OFFSET {offset}
                """
                ).fetchdf()["id"]
            )

            # Build batch results
            batch_results = []

            # Add covered POIs
            for poi_id, aoi_id in covered_pois_result:
                aoi_index = aoi_id_to_index[aoi_id]
                batch_results.append((poi_dict[poi_id], aoi_index, 0))

            # Add neighboring POIs
            for poi_id, aoi_id, _ in neighboring_pois_result:
                aoi_index = aoi_id_to_index[aoi_id]
                batch_results.append((poi_dict[poi_id], aoi_index, 1))

            # Add isolated POIs
            matched_ids = covered_poi_ids | neighboring_poi_ids
            for poi_id in batch_poi_ids:
                if poi_id not in matched_ids:
                    batch_results.append((poi_dict[poi_id], 2))

            all_results.extend(batch_results)
            logging.info(
                f"Batch {offset//batch_size + 1}: {len(batch_results)} POIs processed"
            )

        # Verify no duplicates
        poi_counts = {}
        for result in all_results:
            poi_id = result[0]["id"]
            poi_counts[poi_id] = poi_counts.get(poi_id, 0) + 1

        duplicates = {pid: count for pid, count in poi_counts.items() if count > 1}
        if duplicates:
            logging.error(f"Found {len(duplicates)} duplicate POI IDs in results")
            # Remove duplicates, keeping first occurrence
            seen = set()
            all_results = [
                r
                for r in all_results
                if not (r[0]["id"] in seen or seen.add(r[0]["id"]))
            ]

        covered_count = sum(1 for r in all_results if len(r) == 3 and r[2] == 0)
        neighboring_count = sum(1 for r in all_results if len(r) == 3 and r[2] == 1)
        isolated_count = sum(
            1 for r in all_results if len(r) == 2 or (len(r) == 3 and r[2] == 2)
        )

        logging.info(
            f"Total matched POIs: {len(all_results)} (covered: {covered_count}, "
            f"neighboring: {neighboring_count}, isolated: {isolated_count})"
        )

        return all_results

    def merge_covered_aois_duckdb(
        self,
        aois: list[dict],
        aoi_merge_grid: float = 15_000,
        cover_gate: float = 0.8,
        simplify_tolerance: float = 0.1,
    ) -> list[dict]:
        """
        Merge covered AOIs using DuckDB spatial operations to avoid multiprocessing serialization issues.

        Args:
            aois: List of AOI dictionaries with 'geo' (Shapely geometry) and other metadata
            aoi_merge_grid: Grid size for spatial partitioning
            cover_gate: Coverage threshold for determining if an AOI is contained
            simplify_tolerance: Tolerance for simplifying geometries

        Returns:
            List of merged AOI dictionaries
        """
        logging.info("Merging Covered AOI using DuckDB")

        # Prepare data for DuckDB
        aois_data = []
        for idx, aoi in enumerate(aois):
            geo = aoi["geo"]
            centroid = geo.centroid

            aois_data.append(
                {
                    "idx": idx,
                    "wkt": geo.wkt,
                    "centroid_x": centroid.x,
                    "centroid_y": centroid.y,
                    "area": geo.area,
                    "length": geo.length,
                    "is_valid": geo.is_valid,
                    "grid_x": int(centroid.x // aoi_merge_grid),
                    "grid_y": int(centroid.y // aoi_merge_grid),
                    # Store metadata as JSON for preservation
                    "external": str(aoi.get("external", {})),
                    "id": aoi.get("id", -1),
                }
            )

        # Create DataFrame and register with DuckDB
        df = pd.DataFrame(aois_data)
        self.con.register("aois_temp", df)

        # Create AOI table with geometries
        logging.info("Creating AOI spatial table")
        self.con.execute(
            """
            CREATE OR REPLACE TABLE aois_spatial AS
            SELECT 
                idx,
                ST_GeomFromText(wkt) AS geom,
                centroid_x,
                centroid_y,
                area,
                length,
                is_valid,
                grid_x,
                grid_y,
                external,
                id
            FROM aois_temp
        """
        )

        # Create spatial index
        self.con.execute("CREATE INDEX idx_aois_geom ON aois_spatial USING RTREE(geom)")

        # Find parent-child relationships using SQL
        logging.info("Finding parent-child relationships")
        SQRT2 = 2**0.5

        self.con.execute(
            f"""
            CREATE OR REPLACE TABLE aoi_parents AS
            WITH aoi_pairs AS (
                SELECT 
                    a1.idx AS child_idx,
                    a2.idx AS parent_idx,
                    a1.area AS child_area,
                    a2.area AS parent_area,
                    a1.geom AS child_geom,
                    a2.geom AS parent_geom,
                    a1.centroid_x AS child_x,
                    a1.centroid_y AS child_y,
                    a2.centroid_x AS parent_x,
                    a2.centroid_y AS parent_y,
                    a2.length AS parent_length,
                    a1.is_valid AS child_valid,
                    a2.is_valid AS parent_valid
                FROM aois_spatial a1
                JOIN aois_spatial a2 
                    ON a1.grid_x = a2.grid_x 
                    AND a1.grid_y = a2.grid_y
                    AND a1.idx != a2.idx
                    AND a2.area > a1.area
                    AND a1.is_valid = true
                    AND a2.is_valid = true
            ),
            covered_aois AS (
                SELECT 
                    child_idx,
                    parent_idx,
                    child_area,
                    ST_Area(ST_Intersection(child_geom, parent_geom)) AS intersection_area
                FROM aoi_pairs
                WHERE {SQRT2} * (ABS(child_x - parent_x) + ABS(child_y - parent_y)) < parent_length
                    AND child_valid AND parent_valid
            )
            SELECT 
                child_idx AS idx,
                FIRST(parent_idx) AS parent,
                true AS has_parent
            FROM covered_aois
            WHERE intersection_area > {cover_gate} * child_area
            GROUP BY child_idx
        """
        )

        # Get results
        logging.info("Processing parent-child relationships")
        parent_info = self.con.execute(
            """
            SELECT idx, parent, has_parent
            FROM aoi_parents
        """
        ).fetchall()

        # Build parent mapping
        child2parent = {row[0]: row[1] for row in parent_info}

        # Resolve transitive parent relationships
        for child, parent in list(child2parent.items()):
            while parent in child2parent:
                parent = child2parent[parent]
            child2parent[child] = parent

        # Group children by parent
        from collections import defaultdict

        parent2children = defaultdict(list)
        for child, parent in child2parent.items():
            parent2children[parent].append(child)

        # Update parent AOIs with merged data
        logging.info("Merging child AOI data into parents")
        for parent_idx, children_indices in parent2children.items():
            parent_aoi = aois[parent_idx]
            external = parent_aoi.get("external", {})

            if "inner_poi" not in external:
                external["inner_poi"] = []
            if "population" not in external:
                external["population"] = 0
            if "land_types" not in external:
                external["land_types"] = {}
            if "names" not in external:
                external["names"] = {}

            for child_idx in children_indices:
                child_aoi = aois[child_idx]
                child_external = child_aoi.get("external", {})

                external["inner_poi"].extend(child_external.get("inner_poi", []))
                external["population"] += child_external.get("population", 0)

                for land_type, area in child_external.get("land_types", {}).items():
                    external["land_types"][land_type] = (
                        external["land_types"].get(land_type, 0) + area
                    )

                for name, area in child_external.get("names", {}).items():
                    external["names"][name] = external["names"].get(name, 0) + area

            parent_aoi["external"] = external

        # Filter out children, keep only parents
        has_parent_set = set(child2parent.keys())
        aois_filtered = [
            aoi for idx, aoi in enumerate(aois) if idx not in has_parent_set
        ]

        logging.info(
            f"Reduced from {len(aois)} to {len(aois_filtered)} AOIs after merging"
        )

        # Now find overlaps
        logging.info("Finding overlapping AOIs")

        # Re-index filtered AOIs
        for new_idx, aoi in enumerate(aois_filtered):
            aoi["idx"] = new_idx

        # Create new table with filtered AOIs
        aois_filtered_data = []
        for aoi in aois_filtered:
            geo = aoi["geo"]
            centroid = geo.centroid
            aois_filtered_data.append(
                {
                    "idx": aoi["idx"],
                    "wkt": geo.wkt,
                    "centroid_x": centroid.x,
                    "centroid_y": centroid.y,
                    "area": geo.area,
                    "length": geo.length,
                    "is_valid": geo.is_valid,
                    "grid_x": int(centroid.x // aoi_merge_grid),
                    "grid_y": int(centroid.y // aoi_merge_grid),
                }
            )

        df_filtered = pd.DataFrame(aois_filtered_data)
        self.con.register("aois_filtered_temp", df_filtered)

        self.con.execute(
            """
            CREATE OR REPLACE TABLE aois_filtered AS
            SELECT 
                idx,
                ST_GeomFromText(wkt) AS geom,
                centroid_x,
                centroid_y,
                area,
                length,
                is_valid,
                grid_x,
                grid_y
            FROM aois_filtered_temp
        """
        )

        # Find overlaps
        self.con.execute(
            f"""
            CREATE OR REPLACE TABLE aoi_overlaps AS
            SELECT 
                a1.idx AS aoi_idx,
                a2.idx AS overlap_idx
            FROM aois_filtered a1
            JOIN aois_filtered a2 
                ON a1.grid_x = a2.grid_x 
                AND a1.grid_y = a2.grid_y
                AND a1.idx != a2.idx
                AND a2.area > a1.area
                AND a1.is_valid = true
                AND a2.is_valid = true
            WHERE {SQRT2} * (ABS(a1.centroid_x - a2.centroid_x) + ABS(a1.centroid_y - a2.centroid_y)) 
                < 2 * (a1.length + a2.length)
                AND ST_Intersects(a1.geom, a2.geom)
        """
        )

        # Get overlap information
        overlap_results = self.con.execute(
            """
            SELECT aoi_idx, overlap_idx
            FROM aoi_overlaps
            ORDER BY aoi_idx, overlap_idx
        """
        ).fetchall()

        # Build overlap dictionary
        aoi_overlaps = defaultdict(list)
        for aoi_idx, overlap_idx in overlap_results:
            aoi_overlaps[aoi_idx].append(overlap_idx)

        # Add overlaps to AOI objects
        for aoi in aois_filtered:
            aoi["overlaps"] = aoi_overlaps.get(aoi["idx"], [])

        # Process overlaps - cut overlapping geometries
        logging.info("Processing overlapping geometries")
        has_overlap_aids = defaultdict(list)
        for i, aoi in enumerate(aois_filtered):
            for j in aoi["overlaps"]:
                has_overlap_aids[j].append(i)

        # Use DuckDB for geometry difference operations
        for i, aids in tqdm(has_overlap_aids.items(), desc="Cutting overlaps"):
            aoi = aois_filtered[i]
            for j in aids:
                overlap_aoi = aois_filtered[j]

                # Use DuckDB for difference operation
                try:
                    result = self.con.execute(
                        f"""
                        SELECT ST_AsText(ST_Difference(
                            ST_GeomFromText('{aoi["geo"].wkt}'),
                            ST_GeomFromText('{overlap_aoi["geo"].wkt}')
                        )) AS diff_wkt
                    """
                    ).fetchone()

                    if result and result[0]:

                        diff_geo = shapely_wkt.loads(result[0])

                        if diff_geo and not diff_geo.is_empty:

                            if isinstance(diff_geo, Polygon):
                                aoi["geo"] = diff_geo
                            elif isinstance(diff_geo, MultiPolygon):
                                # Take the largest part
                                candidate_geos = [
                                    (p.area, p) for p in diff_geo.geoms if p
                                ]
                                if candidate_geos:
                                    aoi["geo"] = max(
                                        candidate_geos, key=lambda x: x[0]
                                    )[1]
                except Exception as e:
                    logging.warning(f"Failed to compute difference for AOI {i}: {e}")
                    continue

        # Cleanup
        try: 
          self.con.execute("DROP TABLE IF EXISTS aois_temp")
          self.con.execute("DROP TABLE IF EXISTS aois_spatial")
          self.con.execute("DROP TABLE IF EXISTS aoi_parents")
          self.con.execute("DROP TABLE IF EXISTS aois_filtered_temp")
          self.con.execute("DROP TABLE IF EXISTS aois_filtered")
          self.con.execute("DROP TABLE IF EXISTS aoi_overlaps")
        except Exception as e:
          logging.warning(f"Failed to clean up temporary tables: {e}")

        logging.info(f"Final AOI count: {len(aois_filtered)}")
        return aois_filtered


