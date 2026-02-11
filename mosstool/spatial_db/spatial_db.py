import logging
import duckdb
import pandas as pd
import tempfile
import atexit
import os


class SpatialDB:
    def __init__(self, memory_limit="32GB"):
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

    def _ingest_pois_and_aois(self, pois: list[dict], aois: list[dict]):
        """
        Ingest the pois and aois into the spatial database for efficient spatial queries.
        This is used for matching the pois to the aois.
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

        # Create AOI table with polygon geometries
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

        self.con.execute(
            """
        CREATE TABLE aois AS
        SELECT
            id,
            external,
            ST_GeomFromText(wkt) AS geom
        FROM aois_wkt_df;
        """
        )

        # Create spatial indices
        self.con.execute("CREATE INDEX idx_pois_geom ON pois USING RTREE(geom);")
        self.con.execute("CREATE INDEX idx_aois_geom ON aois USING RTREE(geom);")

        logging.info("Ingested POIs and AOIs into spatial database")
        logging.info(
            f"POI sample: {self.con.execute('SELECT * FROM pois LIMIT 3;').fetchall()}"
        )
        logging.info(
            f"AOI sample: {self.con.execute('SELECT id, ST_AsText(geom), external FROM aois LIMIT 3;').fetchall()}"
        )

    def match_pois_to_aois(
        self,
        pois: list[dict],
        aois: list[dict],
        distance_threshold: float = 18.0,
    ):
        """
        Matches POIs to AOIs using DuckDB spatial logic.
        Replaces the iterative _match_poi_unit function.

        Returns:
            List of tuples:
            - (poi_dict, aoi_index, 0) for covered POIs
            - (poi_dict, aoi_index, 1) for neighboring/projected POIs
            - (poi_dict, 2) for isolated POIs
        """
        self._ingest_pois_and_aois(pois, aois)

        # Step 1: Get POIs covered by AOIs (classification = 0)
        covered_pois_result = self.execute(
            """
            SELECT p.id, a.id AS aoi_id
            FROM pois p
            JOIN aois a ON ST_Within(p.geom, a.geom)
            """
        ).fetchall()

        logging.info(f"Covered {len(covered_pois_result)} POIs by AOIs")

        # Create set for fast lookup
        covered_poi_ids = {poi_id for poi_id, _ in covered_pois_result}

        # Step 2: Get neighboring POIs (within distance threshold but not covered)
        neighboring_pois_result = self.execute(
            f"""
            SELECT p.id, a.id AS aoi_id, ST_Distance(p.geom, a.geom) as distance
            FROM pois p
            JOIN aois a ON ST_DWithin(p.geom, a.geom, {distance_threshold})
            WHERE NOT ST_Within(p.geom, a.geom)
            """
        ).fetchall()

        # Filter to find the closest AOI for each neighboring POI
        poi_to_closest_aoi = {}
        for poi_id, aoi_id, distance in neighboring_pois_result:
            if (
                poi_id not in poi_to_closest_aoi
                or distance < poi_to_closest_aoi[poi_id][1]
            ):
                poi_to_closest_aoi[poi_id] = (aoi_id, distance)

        neighboring_poi_ids = {poi_id for poi_id in poi_to_closest_aoi.keys()}

        logging.info(f"Neighboring {len(neighboring_poi_ids)} POIs near AOIs")

        # Create lookup dictionaries
        poi_dict = {poi["id"]: poi for poi in pois}
        aoi_id_to_index = {aoi["id"]: idx for idx, aoi in enumerate(aois)}

        # Build results
        results = []

        # Add covered POIs: (poi_dict, aoi_index, 0)
        for poi_id, aoi_id in covered_pois_result:
            aoi_index = aoi_id_to_index[aoi_id]
            results.append((poi_dict[poi_id], aoi_index, 0))

        # Add neighboring POIs: (poi_dict, aoi_index, 1)
        for poi_id, (aoi_id, _) in poi_to_closest_aoi.items():
            aoi_index = aoi_id_to_index[aoi_id]
            results.append((poi_dict[poi_id], aoi_index, 1))

        # Add POIs that weren't matched at all: (poi_dict, 2)
        all_matched_ids = covered_poi_ids | neighboring_poi_ids
        for poi in pois:
            if poi["id"] not in all_matched_ids:
                results.append((poi, 2))

        logging.info(
            f"Total matched POIs: {len(results)} (covered: {len(covered_pois_result)}, neighboring: {len(neighboring_poi_ids)}, isolated: {len(results) - len(covered_pois_result) - len(neighboring_poi_ids)})"
        )

        return results


# 2026-02-11 06:45:12,593 - INFO - Sample poi: [{'id': 0, 'coords': [(2150.8059514097986, 686.7657546492173)], 'name': 'Carrefour', 'category': 'amenity|fuel', 'external': {'name': 'Carrefour', 'catg': 'amenity|fuel'}}, {'id': 1, 'coords': [(-1767.8153798340832, 2471.9105316358464)], 'name': "Mairie d'Igny", 'category': 'amenity|townhall', 'external': {'name': "Mairie d'Igny", 'catg': 'amenity|townhall'}}].

#  Sample aoi: [{'id': 0, 'coords': [(-415.66671420305437, 895.4331339390087), (-391.90378178541846, 895.9874454271227), (-363.5794555993215, 898.0984225735215), (-346.06999085623033, 897.6524969421165), (-352.17490207946963, 919.0041349268339), (-362.7668701462501, 949.4748356649122), (-373.8733522090645, 985.1722027834993), (-376.74178554330445, 995.9592295845381), (-383.8778369597901, 996.6269404030702), (-407.64077865025456, 990.0675527703313), (-446.1177293071916, 979.5060556964389), (-448.61912019256, 978.616619382788), (-448.9874421381907, 972.6116069111966), (-447.15016705841595, 948.3688839515622), (-443.3265934394976, 923.1251649121397), (-437.5169092893462, 894.1003423305511), (-415.66671420305437, 895.4331339390087)], 'external': {'population': 0, 'inner_poi': [], 'inner_poi_catg': [], 'osm_tags': [{'access': 'customers', 'amenity': 'parking', 'fee': 'no', 'parking': 'surface'}], 'land_types': {}}, 'geo': <POLYGON ((-415.667 895.433, -391.904 895.987, -363.579 898.098, -346.07 897...>, 'point': (-401.6432523418365, 940.9277419949635), 'length': 354.4263265354603, 'area': 7853.709629768692}, {'id': 1, 'coords': [(-1734.023854801422, 2102.0330848057083), (-1774.2365399995942, 2168.6572773231665), (-1777.25039985689, 2174.32967157472), (-1778.0533057121618, 2193.790748759174), (-1782.3117024715627, 2217.812317124781), (-1781.4282658998513, 2220.369744580731), (-1759.6551780465995, 2225.811898721131), (-1670.4300216273982, 2248.359158206605), (-1664.919267343975, 2229.4527183630457), (-1666.9054643833172, 2228.45246622369), (-1668.1562097356348, 2227.229586483407), (-1667.7154278789683, 2225.4501800326475), (-1667.4216503291095, 2224.004431462702), (-1665.5097964217666, 2222.3357925498385), (-1662.7885071396186, 2221.890165916644), (-1648.0191490581656, 2172.288507875898), (-1651.3293755366694, 2171.066230281592), (-1653.4633192162935, 2167.953127748629), (-1652.6551581001108, 2164.839157959759), (-1651.9203849662065, 2162.2812334025107), (-1648.243272448187, 2160.5008759852685), (-1644.933301439642, 2160.833517897433), (-1635.160615924976, 2127.1356372881705), (-1677.9716584413577, 2116.8062513364125), (-1676.8697890701303, 2112.0241200978116), (-1688.1243591070966, 2109.0249711712227), (-1689.4468460056335, 2113.9183757694595), (-1732.6262695013045, 2102.2550620783586), (-1734.023854801422, 2102.0330848057083)], 'external': {'population': 0, 'inner_poi': [], 'inner_poi_catg': [], 'osm_tags': [{'landuse': 'cemetery', 'name': "Cimetière Communal d'Igny", 'opening_hours': 'Nov-Mar 08:45-17:30;Apr-Oct 8:45-19:00', 'wikidata': 'Q110338395'}], 'land_types': {}}, 'geo': <POLYGON ((-1734.024 2102.033, -1774.237 2168.657, -1777.25 2174.33, -1778.0...>, 'point': (-1709.346762397296, 2174.826715920955), 'length': 496.80382825388784, 'area': 14466.088159117811}]
