#!/usr/bin/env python3
"""Audit the AMIS geodatabase and PDEM raster without modifying raw data."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from pathlib import Path

import fiona
import geopandas as gpd
from pyproj import CRS as PyprojCRS
import rasterio


KEYWORDS = (
    "hazard", "status", "class", "type", "commodity", "jurisdiction",
    "condition", "depth", "date",
)
DATE_FIELD_TERMS = ("date", "updated", "update", "modified", "revision")
CURRENCY_FIELD_TERMS = ("currency", "dollar", "cost", "price", "value", "amount")


def epsg_text(crs: object) -> str:
    """Return direct or component EPSG identifier(s) for a CRS."""
    try:
        parsed = PyprojCRS.from_user_input(crs)
    except Exception:
        return "No EPSG identifier"
    epsg = parsed.to_epsg()
    if epsg is not None:
        return f"EPSG:{epsg}"
    components = []
    for component in parsed.sub_crs_list:
        component_epsg = component.to_epsg()
        if component_epsg is None:
            continue
        role = "horizontal" if component.is_projected else "vertical"
        components.append(f"EPSG:{component_epsg} ({role})")
    return "; ".join(components) or "No EPSG identifier"


def markdown_value(value: object) -> str:
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text if text else "(empty string)"


def matching_columns(columns: Iterable[str]) -> list[str]:
    return [column for column in columns if any(term in column.lower() for term in KEYWORDS)]


def suspected_fields(columns: Iterable[str], terms: tuple[str, ...]) -> list[str]:
    return [column for column in columns if any(term in column.lower() for term in terms)]


def layer_report(gdb_path: Path, layer_name: str) -> list[str]:
    frame = gpd.read_file(gdb_path, layer=layer_name)
    geometry_types = sorted(
        str(value) for value in frame.geometry.geom_type.dropna().unique()
    )
    lines = [
        f"## Layer: `{layer_name}`",
        "",
        f"- Feature count: {len(frame):,}",
        f"- Geometry type(s): {', '.join(geometry_types) or 'None'}",
        f"- CRS: `{frame.crs}`",
        f"- EPSG: {epsg_text(frame.crs)}",
        "",
        "### Columns",
        "",
        "| Column | dtype | Null count |",
        "|---|---|---:|",
    ]
    for column in frame.columns:
        lines.append(
            f"| `{column}` | `{frame[column].dtype}` | {int(frame[column].isna().sum()):,} |"
        )

    lines.extend(["", "### Fields that may be currency or recency dates", ""])
    currency = suspected_fields(frame.columns, CURRENCY_FIELD_TERMS)
    dates = suspected_fields(frame.columns, DATE_FIELD_TERMS)
    lines.append(f"- Currency-like: {', '.join(f'`{x}`' for x in currency) or 'None detected'}")
    lines.append(f"- Last-updated/date-like: {', '.join(f'`{x}`' for x in dates) or 'None detected'}")

    for column in matching_columns(frame.columns):
        values = frame[column].dropna()
        counts = values.value_counts(dropna=True)
        lines.extend([
            "",
            f"### Distinct values: `{column}`",
            "",
            f"- Distinct non-null values: {len(counts):,}",
            "",
            "| Value | Count |",
            "|---|---:|",
        ])
        if counts.empty:
            lines.append("| _No non-null values_ | 0 |")
        else:
            for value, count in counts.head(15).items():
                lines.append(f"| {markdown_value(value)} | {count:,} |")
    return lines


def raster_report(raster_path: Path) -> list[str]:
    with rasterio.open(raster_path) as dataset:
        resolution = ", ".join(str(value) for value in dataset.res)
        bounds = dataset.bounds
        return [
            "## PDEM raster",
            "",
            f"- File: `{raster_path}`",
            f"- CRS: `{dataset.crs}`",
            f"- EPSG: {epsg_text(dataset.crs)}",
            f"- Resolution (x, y): {resolution}",
            f"- Pixel dimensions (width × height): {dataset.width:,} × {dataset.height:,}",
            f"- Bounds: left={bounds.left}, bottom={bounds.bottom}, right={bounds.right}, top={bounds.top}",
            f"- Nodata value: `{dataset.nodata}`",
        ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_dir", nargs="?", type=Path, default=Path("data/raw"))
    parser.add_argument("--output", type=Path, default=Path("notes/schema_findings.md"))
    args = parser.parse_args()

    gdbs = sorted(args.raw_dir.rglob("*.gdb"))
    rasters = sorted([*args.raw_dir.rglob("*.tif"), *args.raw_dir.rglob("*.tiff")])
    if len(gdbs) != 1:
        raise SystemExit(f"Expected exactly one .gdb below {args.raw_dir}; found {len(gdbs)}.")
    if len(rasters) != 1:
        raise SystemExit(f"Expected exactly one PDEM TIFF below {args.raw_dir}; found {len(rasters)}.")

    gdb_path = gdbs[0]
    layers = list(fiona.listlayers(gdb_path))
    if not layers:
        raise SystemExit(f"No layers found in {gdb_path}.")

    report = ["# Phase 0 schema findings", "", "## AMIS geodatabase", "", f"- File: `{gdb_path}`", f"- Layer names: {', '.join(f'`{layer}`' for layer in layers)}", ""]
    for layer in layers:
        report.extend(layer_report(gdb_path, layer))
        report.append("")
    report.extend(raster_report(rasters[0]))
    report.append("")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(report), encoding="utf-8")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
