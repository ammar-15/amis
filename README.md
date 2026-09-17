# AMIS Hazard Visualization

This project turns Ontario Abandoned Mine Information System (AMIS) hazard features into a compact 3D terrain scene for a selected mining district. It reads raw AMIS and PDEM inputs, scores hazard features, converts them to scene coordinates, and writes a minimal set of contract files that a Blender render step can consume.

The repository is organized around a two-stage workflow:

1. Phase 1: geospatial processing in a GIS Python environment
2. Phase 2: Blender rendering from generated contract files

## What the project does

- Reads AMIS features and sites from the geodatabase in `data/raw/amis/AMIS_JUNE_2026.gdb`
- Filters to a configured region or district
- Joins site metadata to hazard features
- Clips and resamples the PDEM terrain raster to a square region
- Fills nodata holes and writes a terrain heightmap
- Samples elevation beneath each hazard point
- Produces risk and confidence scores from configuration weights
- Exports a contract package to `data/processed/` for Blender
- Builds a rendered still image in `renders/`

## Repository structure

- `PRD.md` — product requirements and project rules
- `AGENTS.md` — operational pointer for the repo
- `config.yaml` — region, CRS, terrain, scoring, and filter configuration
- `requirements.txt` — GIS environment dependencies
- `scripts/00_inspect.py` — schema and CRS audit for raw GIS data
- `scripts/01_process_geo.py` — terrain + marker contract generation
- `scripts/02_build_scene.py` — Blender scene generation and render
- `data/raw/` — read-only source data; never write here
- `data/interim/` — intermediate artifacts if needed
- `data/processed/` — generated contract files consumed by Blender
- `renders/` — render outputs
- `notes/` — project findings and analysis notes
- `tests/test_process_geo.py` — synthetic phase-1 validation tests

## Environment setup

This project intentionally uses two separate Python environments:

### GIS environment

Use the repo virtual environment at `.venv`.

```bash
cd /Users/akuul15/repos/amis
source .venv/bin/activate
python -m pip install -r requirements.txt
```

This environment is used for:

- `scripts/00_inspect.py`
- `scripts/01_process_geo.py`

### Blender environment

Do not install GIS libraries into Blender’s bundled Python. The Blender pipeline should be run with Blender’s own environment and should consume the generated contract files rather than reusing GIS logic.

## Run the geospatial pipeline

From the repo root:

```bash
source .venv/bin/activate
python scripts/01_process_geo.py --config config.yaml
```

Optional explicit raster selection:

```bash
python scripts/01_process_geo.py --config config.yaml --raster data/raw/pdem/your_raster.tif --as-of 2026-09-10
```

Expected output artifacts in `data/processed/`:

- `heightmap.png`
- `terrain_meta.json`
- `markers.json`

## Render the final scene

Run Blender headlessly:

```bash
blender --background --python scripts/02_build_scene.py
```

The render step reads the three contract files and writes still images into `renders/`.

## Data rules and constraints

- Raw data in `data/raw/` is read-only.
- Geographic inputs are treated as authoritative; do not modify source files.
- Phase 1 must be idempotent when rerun with the same config and input set.
- `config.yaml` contains the scoring and terrain settings that drive the map logic.
- The project is intentionally focused on correctness and geospatial fidelity rather than a large UI or web deployment.

## Notes

This project is designed as a portfolio-quality geospatial visualization. The main concern is that the terrain surface and markers align correctly in scene space, not simply that the output looks visually impressive.

For more detail on the intended behavior, scoring rationale, and data conventions, see [PRD.md](PRD.md).
