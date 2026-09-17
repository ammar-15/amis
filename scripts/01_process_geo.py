#!/usr/bin/env python3
"""Phase 1: selected AMIS features and a PDEM window -> three scene contract files.

Run with the GIS .venv. See notes/phase1_findings.md for scoring and coordinate
conventions. Raw inputs are opened read-only; no Blender dependencies are used.
"""

from __future__ import annotations

import argparse
import base64
from datetime import date, datetime, timezone
import json
import logging
from pathlib import Path
import tempfile
import zlib

import geopandas as gpd
import numpy as np
from PIL import Image
from pyproj import CRS, Transformer
import rasterio
from rasterio.enums import Resampling
from rasterio.fill import fillnodata
from rasterio.windows import Window, from_bounds
from rasterio.transform import from_bounds as grid_from_bounds
from rasterio.warp import reproject
import yaml


ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("phase1")
FEATURE_LAYER = "AMIS_FEATURES_2026"
SITE_LAYER = "AMIS_SITES_2026"
COMPONENTS = {"has_hazard_status", "has_depth", "has_dimensions", "has_condition_note"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def count_step(label: str, frame: gpd.GeoDataFrame) -> None:
    LOG.info("%s: %s features", label, f"{len(frame):,}")
    require(len(frame) > 0, f"{label}: zero features; stopping.")


def unit_weight(value: object, name: str) -> None:
    require(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and np.isfinite(value) and 0 <= value <= 1,
        f"{name} must be a finite number in [0, 1].",
    )


def validate_config(config: dict) -> None:
    require(isinstance(config, dict), "Config must be a mapping.")
    bbox = config["region"].get("bbox_wgs84")
    if bbox is None:
        require(isinstance(config["region"].get("district"), str)
                and bool(config["region"]["district"].strip()), "region.district is required without a bbox.")
    else:
        require(isinstance(bbox, list) and len(bbox) == 4,
                "region.bbox_wgs84 must be [xmin, ymin, xmax, ymax].")
        require(all(isinstance(value, (int, float)) and not isinstance(value, bool)
                    and np.isfinite(value) for value in bbox), "BBox values must be finite numbers.")
        xmin, ymin, xmax, ymax = bbox
        require(-180 <= xmin < xmax <= 180 and -90 <= ymin < ymax <= 90,
                "BBox must have ordered WGS84 longitude/latitude bounds.")
    require(config["crs"]["source_epsg"] == 4326, "AMIS source CRS must be EPSG:4326.")
    require(config["crs"]["target_epsg"] == 3162, "PDEM horizontal CRS must be EPSG:3162.")
    terrain = config["terrain"]
    require(type(terrain["raster_px"]) is int and terrain["raster_px"] >= 2,
            "terrain.raster_px must be an integer >= 2.")
    for key in ("scene_extent", "vertical_exaggeration"):
        require(np.isfinite(terrain[key]) and terrain[key] > 0, f"terrain.{key} must be positive.")
    severity = config["severity"]
    for name in ("feature_type_weights", "feature_class_multipliers", "hazard_status_multipliers"):
        require(isinstance(severity[name], dict) and bool(severity[name]), f"severity.{name} is required.")
        for key, value in severity[name].items():
            unit_weight(value, f"severity.{name}.{key}")
    unit_weight(severity["feature_type_default"], "severity.feature_type_default")
    depth = severity["depth"]
    require(type(depth["treat_zero_as_missing"]) is bool, "depth.treat_zero_as_missing must be boolean.")
    require(np.isfinite(depth["cap_m"]) and depth["cap_m"] > 0, "depth.cap_m must be positive.")
    for name in ("weight", "missing_depth_assumption"):
        unit_weight(depth[name], f"depth.{name}")
    components = config["confidence"]["components"]
    require(set(components) == COMPONENTS, f"Confidence components must be exactly {sorted(COMPONENTS)}.")
    for name, value in components.items():
        unit_weight(value, f"confidence.components.{name}")
    require(np.isclose(sum(components.values()), 1), "Confidence component weights must sum to 1.")
    recency = config["confidence"]["recency"]
    require(type(recency["enabled"]) is bool, "recency.enabled must be boolean.")
    full, zero = recency["full_confidence_before_years"], recency["zero_confidence_after_years"]
    require(np.isfinite(full) and np.isfinite(zero) and 0 <= full < zero,
            "Recency thresholds must satisfy 0 <= full < zero.")
    require(type(config["filters"]["drop_not_a_hazard"]) is bool,
            "filters.drop_not_a_hazard must be boolean.")
    for value in config["filters"]["null_date_sentinels"]:
        date.fromisoformat(str(value))


def check_points(frame: gpd.GeoDataFrame, layer: str, lon: str, lat: str) -> None:
    require(frame.crs is not None and frame.crs.to_epsg() == 4326,
            f"{layer}: expected EPSG:4326, found {frame.crs}.")
    require(not frame.geometry.isna().any() and not frame.geometry.is_empty.any()
            and frame.geometry.geom_type.eq("Point").all(), f"{layer}: missing or non-point geometry.")
    xy = np.column_stack((frame.geometry.x, frame.geometry.y))
    require(np.isfinite(xy).all(), f"{layer}: non-finite geometry coordinates.")
    require(np.allclose(xy, frame[[lon, lat]].to_numpy(dtype=float), rtol=0, atol=1e-7),
            f"{layer}: geometry does not agree with {lon}/{lat}.")
    require(frame.AMIS_ID.notna().all(), f"{layer}: missing AMIS_ID.")


def clean_dates(frame: gpd.GeoDataFrame, sentinels: list, label: str) -> None:
    """Null configured sentinel dates in memory, preserving every record."""
    sentinels = {str(value) for value in sentinels}
    for column in frame.columns:
        if column.startswith("DATE_"):
            require(hasattr(frame[column], "dt"), f"{label}.{column}: expected a datetime dtype.")
            missing = frame[column].dt.strftime("%Y-%m-%d").isin(sentinels)
            if missing.any():
                LOG.info("%s.%s: nulling %d sentinel dates", label, column, missing.sum())
                frame.loc[missing, column] = None


def load_features(gdb: Path, config: dict) -> gpd.GeoDataFrame:
    features = gpd.read_file(gdb, layer=FEATURE_LAYER)
    count_step("Loaded", features)
    LOG.info("AMIS_DISTRICT counts: %s", features.AMIS_DISTRICT.value_counts().to_dict())
    require(features.crs is not None and features.crs.to_epsg() == config["crs"]["source_epsg"],
            "Feature geometry must be EPSG:4326 before spatial filtering.")
    bbox = config["region"].get("bbox_wgs84")
    if bbox is not None:
        xmin, ymin, xmax, ymax = bbox
        selected = (features.geometry.x.between(xmin, xmax)
                    & features.geometry.y.between(ymin, ymax))
        features = features.loc[selected].copy()
        count_step(f"WGS84 bbox filter {bbox} (district filter disabled)", features)
    else:
        district = config["region"]["district"]
        features = features.loc[features.AMIS_DISTRICT.eq(district)].copy()
        count_step(f"District filter {district!r}", features)
    check_points(features, FEATURE_LAYER, "LONGITUDE_DD", "LATITUDE_DD")
    require(features.FEATURE_ID.notna().all() and features.FEATURE_ID.is_unique,
            "Selected feature IDs must be non-null and unique.")
    if config["filters"]["drop_not_a_hazard"]:
        features = features.loc[features.FEATURE_HAZARD_STATUS.ne("NOT A HAZARD")].copy()
    count_step("Configured hazard filter", features)
    clean_dates(features, config["filters"]["null_date_sentinels"], "features")
    features = features.to_crs(config["crs"]["target_epsg"])
    require(np.isfinite(features.total_bounds).all(), "Reprojection produced non-finite bounds.")
    count_step("Reprojected EPSG:4326 -> EPSG:3162", features)

    sites = gpd.read_file(gdb, layer=SITE_LAYER)
    check_points(sites, SITE_LAYER, "LONGITUDE", "LATITUDE")
    require(sites.AMIS_ID.is_unique, "Sites AMIS_ID is not unique; many-to-one join would duplicate features.")
    clean_dates(sites, config["filters"]["null_date_sentinels"], "sites")
    # Prefix all site context to preserve feature dates, district and geometry.
    context = sites.drop(columns=sites.geometry.name).rename(
        columns={column: f"SITE_{column}" for column in sites.columns
                 if column not in ("AMIS_ID", sites.geometry.name)}
    )
    joined = features.merge(context, on="AMIS_ID", how="left", validate="many_to_one", indicator=True)
    require(joined["_merge"].eq("both").all(), "Features have unmatched AMIS_ID values in sites.")
    require(joined.SITE_OFFICIAL_NAME.notna().all(), "Joined sites have missing official names.")
    require(len(joined) == len(features), "Site join changed the feature count.")
    joined = joined.drop(columns="_merge").sort_values(["AMIS_ID", "FEATURE_ID"]).reset_index(drop=True)
    count_step("Joined sites on AMIS_ID", joined)
    LOG.info("Selected feature types: %s", joined.MINE_FEATURE_TYPE.value_counts().to_dict())
    return joined


def lookup(series, weights: dict, name: str) -> np.ndarray:
    values = series.map(weights)
    require(values.notna().all(), f"Missing configured {name} weights for {series[values.isna()].unique().tolist()}.")
    return values.to_numpy(dtype=float)


def score_features(features: gpd.GeoDataFrame, config: dict, as_of: date) -> tuple[np.ndarray, np.ndarray]:
    severity = config["severity"]
    base = features.MINE_FEATURE_TYPE.map(severity["feature_type_weights"])
    defaults = features.loc[base.isna(), "MINE_FEATURE_TYPE"].value_counts().to_dict()
    LOG.info("Feature types using configured default %s: %s", severity["feature_type_default"], defaults)
    base = base.fillna(severity["feature_type_default"]).to_numpy(dtype=float)
    depth = features.FEATURE_DEPTH_OR_HEIGHT.to_numpy(dtype=float)
    width = features.FEATURE_WIDTH.to_numpy(dtype=float)
    length = features.FEATURE_LENGTH.to_numpy(dtype=float)
    for name, values in (("depth", depth), ("width", width), ("length", length)):
        require(not np.isinf(values).any() and not (values < 0).any(), f"Invalid negative/infinite {name}.")
    depth_config = severity["depth"]
    measured_depth = np.isfinite(depth)
    if depth_config["treat_zero_as_missing"]:
        measured_depth &= depth != 0
    depth_fraction = np.where(measured_depth, np.clip(depth / depth_config["cap_m"], 0, 1),
                              depth_config["missing_depth_assumption"])
    class_factor = lookup(features.MINE_FEATURE_CLASS, severity["feature_class_multipliers"], "class")
    status_factor = lookup(features.FEATURE_HAZARD_STATUS, severity["hazard_status_multipliers"], "status")
    depth_weight = depth_config["weight"]
    score = ((1 - depth_weight) * base + depth_weight * depth_fraction) * class_factor * status_factor

    components = {
        "has_hazard_status": features.FEATURE_HAZARD_STATUS.notna().to_numpy()
        & features.FEATURE_HAZARD_STATUS.ne("NOT AVAILABLE").to_numpy(),
        "has_depth": np.isfinite(depth) & (depth > 0),
        # Config specifies presence here; zero width/length still count as present.
        "has_dimensions": np.isfinite(width) & np.isfinite(length),
        "has_condition_note": features.MINE_FEATURE_CONDITION.notna().to_numpy(),
    }
    confidence = sum(config["confidence"]["components"][key] * present
                     for key, present in components.items())
    recency = config["confidence"]["recency"]
    if recency["enabled"]:
        dates = features.DATE_LAST_MODIFIED_IN_AMIS
        reference = datetime.combine(as_of, datetime.min.time(), tzinfo=timezone.utc)
        age_years = (reference - dates).dt.total_seconds().to_numpy() / (365.2425 * 86400)
        require(not (age_years < 0).any(), "Modification dates are later than --as-of; check the reference date.")
        full, zero = recency["full_confidence_before_years"], recency["zero_confidence_after_years"]
        factor = np.where(np.isfinite(age_years), np.clip((zero - age_years) / (zero - full), 0, 1), 0)
        confidence *= factor
        LOG.info("Recency as of %s: %d missing dates receive zero recency confidence", as_of, dates.isna().sum())
    for name, values in (("severity", score), ("confidence", confidence)):
        require(np.isfinite(values).all() and ((values >= 0) & (values <= 1)).all(), f"Invalid {name} scores.")
        LOG.info("%s min/mean/max: %.6f / %.6f / %.6f", name, values.min(), values.mean(), values.max())
        LOG.info("%s quartiles (25%%, 50%%, 75%%): %s", name, np.quantile(values, [.25, .5, .75]).tolist())
        LOG.info("%s bins [0,.2), [.2,.4), [.4,.6), [.6,.8), [.8,1]: %s", name,
                 np.histogram(values, bins=[0, .2, .4, .6, .8, 1])[0].tolist())
    LOG.info("Depth missing for severity: %d; confidence components present: %s", (~measured_depth).sum(),
             {key: int(value.sum()) for key, value in components.items()})
    count_step("Scored severity and confidence", features)
    return score, confidence


def square_bounds(point_bounds: np.ndarray, margin: float = 0.1) -> np.ndarray:
    """Pad each side, then expand the shorter axis to avoid XY distortion."""
    xmin, ymin, xmax, ymax = np.asarray(point_bounds, dtype=float)
    require(np.isfinite(point_bounds).all(), "Non-finite point bounds.")
    side = max(xmax - xmin, ymax - ymin) * (1 + 2 * margin)
    require(side > 0, "Point extent is degenerate; cannot derive a terrain window.")
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    return np.array([cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2])


def terrain_bounds(features: gpd.GeoDataFrame, config: dict) -> np.ndarray:
    bbox = config["region"].get("bbox_wgs84")
    if bbox is not None:
        # The explicit bbox defines the window; densify curved projected edges.
        projected = Transformer.from_crs(4326, config["crs"]["target_epsg"], always_xy=True).transform_bounds(
            *bbox, densify_pts=21,
        )
        bounds = square_bounds(np.asarray(projected), margin=0)
        LOG.info("Projected configured bbox: %s; expanded to square: %s", projected, bounds.tolist())
    else:
        bounds = square_bounds(features.total_bounds)
        LOG.info("10%% margin per side, shorter axis expanded to square: %s", bounds.tolist())
    return bounds


def fill_nearest(data: np.ma.MaskedArray) -> tuple[np.ndarray, int]:
    values = np.asarray(data, dtype=np.float32).copy()
    valid = ~np.ma.getmaskarray(data) & np.isfinite(values) & (values != 0)
    require(valid.any(), "PDEM window contains no valid elevation pixels.")
    missing = int((~valid).sum())
    if missing:
        require(tuple(map(int, rasterio.__gdal_version__.split(".")[:2])) >= (3, 9),
                "Nearest-neighbour nodata filling requires GDAL >= 3.9 in the GIS environment.")
        values[~valid] = np.nan
        values = fillnodata(values, mask=valid.astype(np.uint8),
                            max_search_distance=float(np.hypot(*values.shape)) + 1,
                            smoothing_iterations=0, interpolation="nearest")
    remaining = int((~np.isfinite(values) | (values == 0)).sum())
    LOG.info("nodata_pixels_filled=%d; nodata_pixels_remaining=%d (%.2f%%)",
             missing - remaining, remaining, 100 * remaining / values.size)
    require(remaining / values.size <= 0.05,
            f"More than 5% of the window is nodata after filling: {remaining}/{values.size}.")
    require(remaining == 0, "Nearest-neighbour fill left missing elevation pixels; refusing mesh holes.")
    return values, missing


def terrain_from_raster(path: Path, bounds: np.ndarray, config: dict) -> tuple[np.ndarray, dict]:
    n = config["terrain"]["raster_px"]
    with rasterio.open(path, "r") as source:
        require(source.count == 1, "Expected a single-band PDEM.")
        compound = CRS.from_user_input(source.crs)
        horizontal = compound.to_2d()
        require(horizontal.to_epsg() == config["crs"]["target_epsg"],
                f"PDEM horizontal CRS {horizontal} does not match config.")
        vertical = [part.to_epsg() for part in compound.sub_crs_list if part.is_vertical]
        require(not vertical or vertical == [5713],
                f"Unexpected declared vertical CRS: {vertical}; expected CGVD28 or unspecified.")
        if not vertical:
            LOG.info("Raster declares no vertical CRS; output vertical datum will be recorded as unknown.")
        transform = source.transform
        require(transform.b == 0 and transform.d == 0 and transform.a > 0 and transform.e < 0,
                "Expected a north-up PDEM raster.")
        xmin, ymin, xmax, ymax = bounds
        coverage = source.bounds
        overlaps = xmax > coverage.left and xmin < coverage.right and ymax > coverage.bottom and ymin < coverage.top
        covered = xmin >= coverage.left and xmax <= coverage.right and ymin >= coverage.bottom and ymax <= coverage.top
        require(covered,
                f"PDEM coverage failure ({'partial overlap' if overlaps else 'NO OVERLAP'}): "
                f"selected window EPSG:3162 {bounds.tolist()} is not covered by {path.name} "
                f"with bounds {list(coverage)}. Supply PDEM coverage for the configured region; "
                "no contract files were written.")
        window = from_bounds(*bounds, transform=transform)
        LOG.info("PDEM window: %s; resampling window to %dx%d (native raster %dx%d)",
                 window, n, n, source.width, source.height)
        # Read only the enclosing native window. Mask ImageServer zero values
        # BEFORE bilinear resampling; otherwise even subpixel bounds rounding
        # blends zeros into spurious positive elevations at coverage edges.
        col0, row0 = int(np.floor(window.col_off)), int(np.floor(window.row_off))
        col1 = min(source.width, int(np.ceil(window.col_off + window.width)))
        row1 = min(source.height, int(np.ceil(window.row_off + window.height)))
        native_window = Window(col0, row0, col1 - col0, row1 - row0)
        native = source.read(1, window=native_window, masked=True).astype(np.float32)
        native = np.ma.masked_where((native == 0) | ~np.isfinite(native), native)
        resampled = np.full((n, n), np.nan, dtype=np.float32)
        reproject(source=native.filled(np.nan), destination=resampled,
                  src_transform=source.window_transform(native_window), src_crs=horizontal,
                  src_nodata=np.nan, dst_transform=grid_from_bounds(*bounds, n, n),
                  dst_crs=horizontal, dst_nodata=np.nan, resampling=Resampling.bilinear)
        data = np.ma.masked_invalid(resampled)
        require(data.count() > 0, "PDEM window contains no valid elevation pixels.")
        low, high = float(data.min()), float(data.max())
        filled_mask = np.ma.getmaskarray(data).copy()
        elevations, missing = fill_nearest(data)
        LOG.info("Resampled PDEM: %d/%d nodata pixels filled by nearest neighbour", missing, n * n)
        require(high > low, "PDEM window has no elevation range; refusing an empty/flat heightmap.")
        pixels = np.rint((elevations.astype(float) - low) / (high - low) * 65535).astype(np.uint16)
        span = float(xmax - xmin)
        meta = {
            "crs_epsg": horizontal.to_epsg(),
            "vertical_crs_epsg": vertical[0] if vertical else None,
            "bounds_source_crs": bounds.tolist(),
            "elevation_min_m": low,
            "elevation_max_m": high,
            "pixel_size_m": span / n,
            "source_pixel_size_m": list(source.res),
            **config["terrain"],
            "nodata_pixels_filled": missing,
            "nodata_pixels_remaining": 0,
            "filled_nodata_mask": {
                "encoding": "base64-zlib-packbits-little",
                "shape": [n, n],
                "row_order": "north_to_south",
                "data": base64.b64encode(zlib.compress(
                    np.packbits(filled_mask.ravel(), bitorder="little").tobytes()
                )).decode("ascii"),
            },
            "heightmap_row_order": "north_to_south",
            "heightmap_sampling": "bilinear_pixel_centers_clamped_at_edges",
            "scene_z_origin_elevation_m": low,
            "scene_units_per_m": config["terrain"]["scene_extent"] / span,
        }
    return pixels, meta


def sample_heightmap(pixels: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """u increases east and v south; sample the actual quantized PNG pixel centers."""
    require(np.isfinite(u).all() and np.isfinite(v).all()
            and ((u >= 0) & (u <= 1) & (v >= 0) & (v <= 1)).all(), "Markers fall outside heightmap bounds.")
    rows, cols = pixels.shape
    x = np.clip(u * cols - 0.5, 0, cols - 1)
    y = np.clip(v * rows - 0.5, 0, rows - 1)
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    x1, y1 = np.minimum(x0 + 1, cols - 1), np.minimum(y0 + 1, rows - 1)
    dx, dy = x - x0, y - y0
    return ((1 - dy) * ((1 - dx) * pixels[y0, x0] + dx * pixels[y0, x1])
            + dy * ((1 - dx) * pixels[y1, x0] + dx * pixels[y1, x1]))


def make_markers(features: gpd.GeoDataFrame, score: np.ndarray, confidence: np.ndarray,
                 pixels: np.ndarray, meta: dict) -> list[dict]:
    xmin, ymin, xmax, ymax = meta["bounds_source_crs"]
    u = (features.geometry.x.to_numpy() - xmin) / (xmax - xmin)
    v = (ymax - features.geometry.y.to_numpy()) / (ymax - ymin)
    height_fraction = sample_heightmap(pixels, u, v) / 65535
    extent = meta["scene_extent"]
    x, y = (u - 0.5) * extent, (0.5 - v) * extent
    z = (height_fraction * (meta["elevation_max_m"] - meta["elevation_min_m"])
         * meta["scene_units_per_m"] * meta["vertical_exaggeration"])
    require(np.isfinite(np.column_stack((x, y, z))).all(), "Non-finite scene coordinates.")
    LOG.info("Scene X [%.6f, %.6f], Y [%.6f, %.6f], Z [%.6f, %.6f]",
             x.min(), x.max(), y.min(), y.max(), z.min(), z.max())
    # An explicit bbox may legitimately contain tightly clustered features.
    require(max(np.ptp(x), np.ptp(y)) > 0, "Marker coordinates unexpectedly collapsed.")
    markers = []
    for index, row in enumerate(features.itertuples(index=False)):
        markers.append({
            "amis_id": str(row.AMIS_ID), "site_name": str(row.SITE_OFFICIAL_NAME),
            "hazard_type": str(row.MINE_FEATURE_TYPE), "hazard_class": str(row.MINE_FEATURE_CLASS),
            "status": str(row.FEATURE_HAZARD_STATUS), "score": float(score[index]),
            "confidence": float(confidence[index]),
            "x": float(x[index]), "y": float(y[index]), "z": float(z[index]),
        })
    count_step("Sampled heightmap and converted to scene coordinates", features)
    require(len(markers) == len(features), "Marker count differs from the post-filter count.")
    return markers


def write_contract(output: Path, pixels: np.ndarray, meta: dict, markers: list[dict]) -> None:
    # Stage all three files before replacement so validation failures leave outputs intact.
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="phase1-", dir=output.parent) as staging:
        staging = Path(staging)
        Image.fromarray(pixels).save(staging / "heightmap.png")
        with Image.open(staging / "heightmap.png") as saved:
            require(np.array_equal(np.asarray(saved), pixels), "Heightmap PNG did not round-trip losslessly.")
        for filename, content in (("terrain_meta.json", meta), ("markers.json", markers)):
            (staging / filename).write_text(json.dumps(content, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        for filename in ("heightmap.png", "terrain_meta.json", "markers.json"):
            (staging / filename).replace(output / filename)


def run(config_path: Path, raster_path: Path | None, as_of: date) -> None:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    validate_config(config)
    gdb = ROOT / "data/raw/amis/AMIS_JUNE_2026.gdb"
    if raster_path is None:
        candidates = sorted(path for path in (ROOT / "data/raw/pdem").rglob("*")
                            if path.suffix.lower() in (".tif", ".tiff") and path.is_file())
        require(len(candidates) == 1, f"Expected one PDEM TIFF; found {len(candidates)}. Select one with --raster.")
        raster_path = candidates[0]
    features = load_features(gdb, config)
    score, confidence = score_features(features, config, as_of)
    LOG.info("Selected projected point bounds: %s", features.total_bounds.tolist())
    bounds = terrain_bounds(features, config)
    pixels, meta = terrain_from_raster(raster_path, bounds, config)
    count_step("Window-read, resampled and filled terrain", features)
    bbox = config["region"].get("bbox_wgs84")
    meta.update({"district": config["region"].get("district") if bbox is None else None,
                 "render": config["render"],
                 "selection_method": "bbox_wgs84" if bbox is not None else "district",
                 "bbox_wgs84": bbox, "scoring_as_of": as_of.isoformat(),
                 "marker_count": len(features)})
    markers = make_markers(features, score, confidence, pixels, meta)
    write_contract(ROOT / "data/processed", pixels, meta, markers)
    LOG.info("Wrote exactly three contract files to data/processed; %d markers", len(markers))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--raster", type=Path, help="PDEM TIFF covering the entire selected window")
    parser.add_argument("--as-of", type=date.fromisoformat, default=datetime.now(timezone.utc).date(),
                        help="Recency reference date YYYY-MM-DD; pin for reproducible scores (default: UTC today)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        run(args.config, args.raster, args.as_of)
    except (ValueError, KeyError, OSError) as error:
        LOG.error("Phase 1 stopped: %s", error)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
