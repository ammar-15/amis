"""Phase 1 checks using small synthetic data; never writes project raw data."""

import copy
import base64
import zlib
from datetime import date, datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import geopandas as gpd
import numpy as np
from PIL import Image
import rasterio
from rasterio.transform import from_bounds
import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("process_geo", ROOT / "scripts/01_process_geo.py")
geo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(geo)
REFERENCE = date(2026, 9, 10)


class Phase1Tests(unittest.TestCase):
    def setUp(self):
        self.config = yaml.safe_load((ROOT / "config.yaml").read_text())
        self.config["region"]["bbox_wgs84"] = None
        self.config["terrain"]["raster_px"] = 16

    def features(self):
        now = datetime(2026, 9, 10, tzinfo=timezone.utc)
        return gpd.GeoDataFrame({
            "AMIS_ID": ["1", "2", "3"], "FEATURE_ID": [1., 2., 3.],
            "AMIS_DISTRICT": ["COBALT"] * 3, "OFFICIAL_NAME": ["feature label"] * 3,
            "SITE_OFFICIAL_NAME": ["Site One", "Site Two", "Site Three"],
            "MINE_FEATURE_TYPE": ["SHAFT - 1 COMPARTMENT - VERTICAL SHAFT"] * 3,
            "MINE_FEATURE_CLASS": ["FEATURE TO SURFACE"] * 3,
            "FEATURE_HAZARD_STATUS": ["ACTIVE", "NOT AVAILABLE", "ACTIVE"],
            "FEATURE_DEPTH_OR_HEIGHT": [50., 0., np.nan],
            "FEATURE_WIDTH": [3., 0., np.nan], "FEATURE_LENGTH": [3., 0., np.nan],
            "MINE_FEATURE_CONDITION": ["Measured", "Not surveyed", None],
            "DATE_LAST_MODIFIED_IN_AMIS": [now, now - timedelta(days=15 * 365.2425), None],
        }, geometry=gpd.points_from_xy([10., 40., 90.], [10., 45., 70.]), crs=3162)

    def write_raster(self, path, bounds):
        # Asymmetric gradient catches north/south reversal; one nodata hole.
        values = (100 + np.arange(16)[:, None] * 7 + np.arange(16)[None, :] * 2).astype("float32")
        values[7, 7] = -9999
        with rasterio.open(path, "w", driver="GTiff", width=16, height=16, count=1,
                           dtype="float32", crs="EPSG:3162+5713", nodata=-9999,
                           transform=from_bounds(*bounds, width=16, height=16)) as dataset:
            dataset.write(values, 1)

    def test_config_validation(self):
        geo.validate_config(self.config)
        for section, key, bad in (("crs", "source_epsg", 4269), ("crs", "target_epsg", 3161),
                                  ("region", "bbox_wgs84", [-79, 46, -80, 47])):
            config = copy.deepcopy(self.config)
            config[section][key] = bad
            with self.assertRaises(ValueError):
                geo.validate_config(config)

    def test_scoring_and_unknown_dates(self):
        severity, confidence = geo.score_features(self.features(), self.config, REFERENCE)
        # Known 50m shaft: 1.0. Unknown depth/status: (.6 + .4*.5)*.8 = .64.
        np.testing.assert_allclose(severity, [1., .64, .8])
        # Presence: 1, .15+.20, .35; recency: 1, .5, 0.
        np.testing.assert_allclose(confidence, [1., .175, 0.])
        config = copy.deepcopy(self.config)
        config["confidence"]["recency"]["enabled"] = False
        new_severity, confidence = geo.score_features(self.features(), config, REFERENCE)
        np.testing.assert_array_equal(new_severity, severity)
        np.testing.assert_allclose(confidence, [1., .35, .35])

    def test_type_default_and_missing_multiplier(self):
        features = self.features()
        features.loc[0, "MINE_FEATURE_TYPE"] = "UNLISTED TYPE"
        severity, _ = geo.score_features(features, self.config, REFERENCE)
        self.assertAlmostEqual(severity[0], .6 * .35 + .4)
        features.loc[0, "MINE_FEATURE_CLASS"] = "UNLISTED CLASS"
        with self.assertRaisesRegex(ValueError, "Missing configured class"):
            geo.score_features(features, self.config, REFERENCE)

    def test_sentinel_and_recency_endpoints(self):
        features = self.features()
        features.loc[0, "DATE_LAST_MODIFIED_IN_AMIS"] = datetime(1899, 12, 30, tzinfo=timezone.utc)
        geo.clean_dates(features, self.config["filters"]["null_date_sentinels"], "test")
        self.assertTrue(features.DATE_LAST_MODIFIED_IN_AMIS.isna().iloc[0])
        now = datetime(2026, 9, 10, tzinfo=timezone.utc)
        features["DATE_LAST_MODIFIED_IN_AMIS"] = [
            now - timedelta(days=5 * 365.2425), now - timedelta(days=25 * 365.2425), now,
        ]
        _, confidence = geo.score_features(features, self.config, REFERENCE)
        np.testing.assert_allclose(confidence, [1., 0., .35])

    def test_district_join_retains_features_and_uses_site_name(self):
        features = self.features().drop(columns="SITE_OFFICIAL_NAME").to_crs(4326)
        features["LONGITUDE_DD"], features["LATITUDE_DD"] = features.geometry.x, features.geometry.y
        features.loc[2, "AMIS_DISTRICT"] = "OTHER"
        sites = gpd.GeoDataFrame({
            "AMIS_ID": ["1", "2", "3"], "OFFICIAL_NAME": ["joined one", "joined two", "joined three"],
            "LONGITUDE": features.geometry.x, "LATITUDE": features.geometry.y,
        }, geometry=features.geometry, crs=4326)
        with patch.object(geo.gpd, "read_file", side_effect=[features, sites]):
            joined = geo.load_features(Path("fixture.gdb"), self.config)
        self.assertEqual(len(joined), 2)
        self.assertEqual(joined.SITE_OFFICIAL_NAME.tolist(), ["joined one", "joined two"])
        self.assertEqual(joined.crs.to_epsg(), 3162)
        config = copy.deepcopy(self.config)
        config["region"]["district"] = "NO SUCH DISTRICT"
        with patch.object(geo.gpd, "read_file", return_value=features):
            with self.assertRaisesRegex(ValueError, "zero features"):
                geo.load_features(Path("fixture.gdb"), config)
        sites.loc[1, "AMIS_ID"] = "1"
        with patch.object(geo.gpd, "read_file", side_effect=[features, sites]):
            with self.assertRaisesRegex(ValueError, "not unique"):
                geo.load_features(Path("fixture.gdb"), self.config)

    def test_margin_square_and_fill(self):
        np.testing.assert_allclose(geo.square_bounds(np.array([0., 0., 100., 50.])), [-10, -35, 110, 85])
        data = np.ma.array([[1., 0., 9.]], mask=[[False, True, False]])
        filled, count = geo.fill_nearest(data)
        self.assertEqual(count, 1)
        self.assertIn(filled[0, 1], (1., 9.))  # nearest donor, never an average
        np.testing.assert_array_equal(filled[0, [0, 2]], [1., 9.])
        with self.assertRaisesRegex(ValueError, "no valid elevation"):
            geo.fill_nearest(np.ma.masked_all((2, 2), dtype="float32"))

    def test_bbox_overrides_district_and_includes_boundary(self):
        self.config["region"]["bbox_wgs84"] = [-79.88, 47.20, -79.48, 47.60]
        geo.validate_config(self.config)
        features = self.features().drop(columns="SITE_OFFICIAL_NAME")
        features = features.set_geometry(gpd.points_from_xy(
            [-79.88, -79.6, -80.], [47.20, 47.5, 47.4], crs=4326))
        features["LONGITUDE_DD"], features["LATITUDE_DD"] = features.geometry.x, features.geometry.y
        features["AMIS_DISTRICT"] = ["OTHER", "OTHER", "COBALT"]
        sites = gpd.GeoDataFrame({
            "AMIS_ID": ["1", "2", "3"], "OFFICIAL_NAME": ["one", "two", "three"],
            "LONGITUDE": features.geometry.x, "LATITUDE": features.geometry.y,
        }, geometry=features.geometry, crs=4326)
        with patch.object(geo.gpd, "read_file", side_effect=[features, sites]):
            joined = geo.load_features(Path("fixture.gdb"), self.config)
        self.assertEqual(joined.AMIS_ID.tolist(), ["1", "2"])
        bounds = geo.terrain_bounds(joined, self.config)
        self.assertAlmostEqual(bounds[2] - bounds[0], bounds[3] - bounds[1])
        # The window follows the configured bbox, not changes to the point subset.
        np.testing.assert_allclose(bounds, geo.terrain_bounds(joined.iloc[:1], self.config))

    def test_pixel_center_sampling(self):
        pixels = np.array([[0, 100], [200, 300]], dtype=np.uint16)
        np.testing.assert_allclose(
            geo.sample_heightmap(pixels, np.array([0., .25, .5, .75, 1.]), np.array([0., .25, .5, .75, 1.])),
            [0, 0, 150, 300, 300],
        )

    def test_contract_round_trip_and_idempotence(self):
        features = self.features()
        bounds = geo.square_bounds(features.total_bounds)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            path = directory / "fixture.tif"
            output = directory / "processed"
            self.write_raster(path, bounds)
            hashes = []
            for _ in range(2):
                pixels, meta = geo.terrain_from_raster(path, bounds, self.config)
                score, confidence = geo.score_features(features, self.config, REFERENCE)
                markers = geo.make_markers(features, score, confidence, pixels, meta)
                geo.write_contract(output, pixels, meta, markers)
                hashes.append({item.name: item.read_bytes() for item in output.iterdir()})
            self.assertEqual(hashes[0], hashes[1])
            self.assertEqual(set(hashes[0]), {"heightmap.png", "terrain_meta.json", "markers.json"})
            # PNG IHDR: bit depth 16, colour type 0 (grayscale).
            self.assertEqual(hashes[0]["heightmap.png"][24:26], bytes([16, 0]))
            with Image.open(output / "heightmap.png") as image:
                self.assertEqual(image.size, (16, 16))
                self.assertEqual(np.asarray(image).min(), 0)
                self.assertEqual(np.asarray(image).max(), 65535)
            saved = json.loads((output / "markers.json").read_text())
            self.assertEqual(len(saved), 3)
            self.assertTrue(all("confidence" in marker for marker in saved))
            self.assertLess(saved[0]["x"], saved[2]["x"])
            self.assertLess(saved[0]["y"], saved[2]["y"])
            # Reconstruct geographic positions and independently interpolate PNG heights.
            for marker, point in zip(saved, features.geometry):
                extent = meta["scene_extent"]
                u, v = marker["x"] / extent + .5, .5 - marker["y"] / extent
                self.assertAlmostEqual(bounds[0] + u * (bounds[2] - bounds[0]), point.x)
                self.assertAlmostEqual(bounds[3] - v * (bounds[3] - bounds[1]), point.y)
                col, row = u * 16 - .5, v * 16 - .5
                c, r = int(col), int(row)
                dc, dr = col - c, row - r
                sample = (pixels[r, c] * (1-dc) * (1-dr) + pixels[r, c+1] * dc * (1-dr)
                          + pixels[r+1, c] * (1-dc) * dr + pixels[r+1, c+1] * dc * dr)
                expected_z = (sample / 65535 * (meta["elevation_max_m"] - meta["elevation_min_m"])
                              * extent / (bounds[2] - bounds[0]) * meta["vertical_exaggeration"])
                self.assertAlmostEqual(marker["z"], expected_z)

    def test_coverage_failure_precedes_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.tif"
            self.write_raster(path, [0, 0, 100, 100])
            for bounds, message in (([0., 200., 100., 300.], "NO OVERLAP"),
                                    ([0., 50., 100., 150.], "partial overlap")):
                with self.assertRaisesRegex(ValueError, message):
                    geo.terrain_from_raster(path, np.array(bounds), self.config)

    def test_horizontal_only_raster_preserves_unknown_vertical_datum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.tif"
            bounds = np.array([0., 0., 100., 100.])
            self.write_raster(path, bounds)
            with rasterio.open(path, "r+") as dataset:
                dataset.crs = "EPSG:3162"
            pixels, meta = geo.terrain_from_raster(path, bounds, self.config)
            self.assertEqual(pixels.shape, (16, 16))
            self.assertEqual(meta["crs_epsg"], 3162)
            self.assertIsNone(meta["vertical_crs_epsg"])

    def test_zero_elevations_excluded_from_statistics_and_filled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.tif"
            bounds = np.array([0., 0., 100., 100.])
            self.write_raster(path, bounds)
            with rasterio.open(path, "r+") as dataset:
                data = dataset.read(1)
                data[:4, :] = 0  # 25% initial nodata must not trigger the post-fill limit.
                dataset.write(data, 1)
            # Mimic the tiny bounds rounding of the real ImageServer clip.
            inset_bounds = bounds + np.array([.00001, .00001, -.00001, -.00001])
            pixels, meta = geo.terrain_from_raster(path, inset_bounds, self.config)
            self.assertAlmostEqual(meta["elevation_min_m"], 128., delta=0.0001)
            self.assertEqual(meta["nodata_pixels_filled"], 65)  # 64 zeros + original hole
            packed = zlib.decompress(base64.b64decode(meta["filled_nodata_mask"]["data"]))
            mask = np.unpackbits(np.frombuffer(packed, dtype=np.uint8), bitorder="little").reshape(16, 16)
            self.assertEqual(int(mask.sum()), 65)
            self.assertTrue(mask[:4].all())
            self.assertEqual(mask[7, 7], 1)
            self.assertEqual(meta["nodata_pixels_remaining"], 0)
            decoded = pixels / 65535 * (meta["elevation_max_m"] - 128) + 128
            self.assertTrue((decoded >= 128).all())

    def test_post_fill_nodata_limit(self):
        data = np.ma.array(np.ones((10, 10), dtype="float32"), mask=False)
        data[0, :] = 0
        for leftover in (np.nan, 0.):
            incomplete = np.ones((10, 10), dtype="float32")
            incomplete[0, :] = leftover
            with patch.object(geo, "fillnodata", return_value=incomplete):
                with self.assertRaisesRegex(ValueError, "More than 5%"):
                    geo.fill_nearest(data)


if __name__ == "__main__":
    unittest.main()
