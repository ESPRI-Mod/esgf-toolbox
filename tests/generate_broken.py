"""
Generate broken variants of golden netCDF files to test failure paths.

Each broken file triggers a specific, realistic error that users may encounter.

Key insight: esgdrs cross-references the FILENAME and GLOBAL ATTRIBUTES.
When only one is wrong, esgdrs falls back to the other and still succeeds.
When BOTH are wrong, esgdrs fails. QA/QC catches issues in all cases.

Run from the repo root:
    uvx --from netCDF4 python tests/generate_broken.py
"""

import netCDF4 as nc
from pathlib import Path
import shutil

GOLDEN = Path(__file__).parent / "golden"
BROKEN = Path(__file__).parent / "broken"

# Golden filenames for reference:
# CMIP7: ta_tavg-p19-hxy-air_day_glb_g101_CNRM-ESM2-1e_piControl_r1i1p1f1_18500101-18500103.nc
# CMIP6: psl_Amon_CESM1-1-CAM5-CMIP5_dcppA-hindcast_s1980-r10i1p1f1_gn_198011-198101.nc


def copy_golden(project: str, broken_name: str, new_filename: str = None) -> Path:
    """Copy a golden file to the broken directory, optionally renaming it."""
    golden_dir = GOLDEN / project
    src = next(golden_dir.glob("*.nc"))
    dst_dir = BROKEN / broken_name
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / (new_filename if new_filename else src.name)
    shutil.copy2(src, dst)
    return dst


# ---------------------------------------------------------------------------
# Category 1: Wrong global attributes (esgdrs succeeds, QA/QC catches them)
#
# These show that esgdrs is filename-driven: it warns about bad attributes
# but still generates the DRS tree from the filename. The data would NOT be
# publishable because of the attribute errors — QA/QC is the gatekeeper.
# ---------------------------------------------------------------------------

def break_wrong_experiment_id():
    """CMIP7 file with an experiment_id not in the controlled vocabulary.

    esgdrs: SUCCEEDS (uses filename 'piControl', ignores attribute)
    QA/QC:  catches it (unknown experiment_id)
    """
    name = "cmip7_wrong_attr_experiment_id"
    f = copy_golden("CMIP7", name)
    with nc.Dataset(f, "r+") as ds:
        ds.experiment_id = "totallyFakeExperiment"
    print(f"  {name}: set experiment_id attr to 'totallyFakeExperiment'")


def break_wrong_source_id():
    """CMIP6 file with a source_id that doesn't exist in the CV.

    esgdrs: SUCCEEDS (uses filename 'CESM1-1-CAM5-CMIP5', ignores attribute)
    QA/QC:  catches it (unknown source_id, filename vs attribute mismatch)
    """
    name = "cmip6_wrong_attr_source_id"
    f = copy_golden("CMIP6", name)
    with nc.Dataset(f, "r+") as ds:
        ds.source_id = "FAKE-MODEL-999"
    print(f"  {name}: set source_id attr to 'FAKE-MODEL-999'")


def break_wrong_frequency():
    """CMIP7 file where frequency attribute doesn't match the filename.

    esgdrs: SUCCEEDS (uses filename 'day', ignores attribute 'mon')
    QA/QC:  catches it (time squareness mismatch, time range precision mismatch)
    """
    name = "cmip7_wrong_attr_frequency"
    f = copy_golden("CMIP7", name)
    with nc.Dataset(f, "r+") as ds:
        ds.frequency = "mon"
    print(f"  {name}: set frequency attr to 'mon' (filename says 'day')")


def break_missing_tracking_id():
    """CMIP7 file missing the tracking_id attribute.

    esgdrs: SUCCEEDS (tracking_id is not used for DRS path)
    QA/QC:  catches it (required attribute missing)
    """
    name = "cmip7_missing_attr_tracking_id"
    f = copy_golden("CMIP7", name)
    with nc.Dataset(f, "r+") as ds:
        if "tracking_id" in ds.ncattrs():
            ds.delncattr("tracking_id")
    print(f"  {name}: removed 'tracking_id' global attribute")


# ---------------------------------------------------------------------------
# Category 2: Wrong filenames (esgdrs FAILS or produces wrong DRS tree)
#
# Since esgdrs parses the filename to build the DRS path, these actually
# break the pipeline.
# ---------------------------------------------------------------------------

def break_filename_wrong_source_id():
    """CMIP7 file renamed with a wrong source_id in the filename.

    esgdrs: SUCCEEDS (falls back to attributes for source_id)
    QA/QC:  catches it (filename vs attribute mismatch)
    """
    name = "cmip7_wrong_filename_source_id"
    new_name = "ta_tavg-p19-hxy-air_day_glb_g101_FAKE-MODEL_piControl_r1i1p1f1_18500101-18500103.nc"
    copy_golden("CMIP7", name, new_name)
    print(f"  {name}: renamed with source_id 'FAKE-MODEL' in filename")


def break_filename_wrong_variable():
    """CMIP7 file renamed with wrong variable name in filename.

    esgdrs: SUCCEEDS but DRS has wrong variable (uses filename 'zg' instead of 'ta')
    QA/QC:  catches it (filename variable vs data variable mismatch)
    """
    name = "cmip7_wrong_filename_variable"
    new_name = "zg_tavg-p19-hxy-air_day_glb_g101_CNRM-ESM2-1e_piControl_r1i1p1f1_18500101-18500103.nc"
    copy_golden("CMIP7", name, new_name)
    print(f"  {name}: renamed variable from 'ta' to 'zg' in filename")


def break_filename_wrong_timerange():
    """CMIP6 file renamed with wrong time range in filename.

    esgdrs: SUCCEEDS (time range not in CMIP6 DRS path)
    QA/QC:  catches it (time range vs actual time axis mismatch)
    """
    name = "cmip6_wrong_filename_timerange"
    new_name = "psl_Amon_CESM1-1-CAM5-CMIP5_dcppA-hindcast_s1980-r10i1p1f1_gn_200001-200101.nc"
    copy_golden("CMIP6", name, new_name)
    print(f"  {name}: changed time range from 198011-198101 to 200001-200101")


# ---------------------------------------------------------------------------
# Category 3: Both filename AND attributes wrong (esgdrs FAILS)
#
# esgdrs cross-references filename and attributes. When both are wrong,
# there is no valid fallback and the file is SKIPPED.
# ---------------------------------------------------------------------------

def break_both_wrong_source_id():
    """CMIP7 file with wrong source_id in BOTH filename and attribute.

    esgdrs: FAILS (no valid source_id from either filename or attribute)
    QA/QC:  catches it
    """
    name = "cmip7_both_wrong_source_id"
    new_name = "ta_tavg-p19-hxy-air_day_glb_g101_FAKE-MODEL_piControl_r1i1p1f1_18500101-18500103.nc"
    f = copy_golden("CMIP7", name, new_name)
    with nc.Dataset(f, "r+") as ds:
        ds.source_id = "FAKE-MODEL"
        ds.institution_id = "FAKE-INST"
    print(f"  {name}: source_id 'FAKE-MODEL' in filename AND attribute")


def break_both_wrong_experiment_id():
    """CMIP7 file with wrong experiment_id in BOTH filename and attribute.

    esgdrs: FAILS (no valid experiment_id from either source)
    QA/QC:  catches it
    """
    name = "cmip7_both_wrong_experiment_id"
    new_name = "ta_tavg-p19-hxy-air_day_glb_g101_CNRM-ESM2-1e_fakeExperiment_r1i1p1f1_18500101-18500103.nc"
    f = copy_golden("CMIP7", name, new_name)
    with nc.Dataset(f, "r+") as ds:
        ds.experiment_id = "fakeExperiment"
    print(f"  {name}: experiment_id 'fakeExperiment' in filename AND attribute")


def break_both_wrong_cmip6():
    """CMIP6 file with wrong source_id in BOTH filename and attribute.

    esgdrs: FAILS (no valid source_id from either source)
    QA/QC:  catches it
    """
    name = "cmip6_both_wrong_source_id"
    new_name = "psl_Amon_FAKE-MODEL_dcppA-hindcast_s1980-r10i1p1f1_gn_198011-198101.nc"
    f = copy_golden("CMIP6", name, new_name)
    with nc.Dataset(f, "r+") as ds:
        ds.source_id = "FAKE-MODEL"
        ds.institution_id = "FAKE-INST"
    print(f"  {name}: source_id 'FAKE-MODEL' in filename AND attribute")


# ---------------------------------------------------------------------------
# Category 4: Corrupted / invalid files
# ---------------------------------------------------------------------------

def break_not_netcdf():
    """A file with .nc extension that is not actually a netCDF file.

    esgdrs: FAILS (InvalidNetCDFFile)
    QA/QC:  FAILS (OSError: Unknown file format)
    """
    name = "not_a_netcdf"
    dst_dir = BROKEN / name
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "fake_data.nc"
    dst.write_text("This is not a netCDF file.\n")
    print(f"  {name}: plain text file with .nc extension")


def break_truncated_netcdf():
    """A netCDF file truncated to half its size (simulates incomplete transfer).

    esgdrs: FAILS (corrupted file)
    QA/QC:  FAILS (corrupted file)
    """
    name = "truncated_netcdf"
    golden_dir = GOLDEN / "CMIP7"
    src = next(golden_dir.glob("*.nc"))
    dst_dir = BROKEN / name
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    data = src.read_bytes()
    dst.write_bytes(data[: len(data) // 2])
    print(f"  {name}: truncated CMIP7 file to {len(data)//2} bytes (half size)")


if __name__ == "__main__":
    if BROKEN.exists():
        shutil.rmtree(BROKEN)

    print("Generating broken variants...\n")

    print("Wrong attributes (esgdrs SUCCEEDS — it uses filename, not attributes):")
    print("  These are still valuable: data won't be publishable, QA/QC catches them.")
    break_wrong_experiment_id()
    break_wrong_source_id()
    break_wrong_frequency()
    break_missing_tracking_id()

    print("\nWrong filenames only (esgdrs SUCCEEDS — falls back to attributes):")
    break_filename_wrong_source_id()
    break_filename_wrong_variable()
    break_filename_wrong_timerange()

    print("\nBoth filename AND attributes wrong (esgdrs FAILS):")
    break_both_wrong_source_id()
    break_both_wrong_experiment_id()
    break_both_wrong_cmip6()

    print("\nCorrupted / invalid files:")
    break_not_netcdf()
    break_truncated_netcdf()

    print(f"\nDone! {len(list(BROKEN.iterdir()))} broken variants in {BROKEN}/")
