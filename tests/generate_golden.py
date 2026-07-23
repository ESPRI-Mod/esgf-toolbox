"""
Generate small synthetic golden netCDF files for each project (CMIP7, CMIP6, CORDEX-CMIP6).
Based on real files in ../test_data/ but subsetted to tiny dimensions and fixed to pass QA/QC.
"""

import netCDF4 as nc
import numpy as np
from pathlib import Path
import shutil

OUTDIR = Path(__file__).parent / "golden"


def make_cmip7_golden():
    """Create a golden CMIP7 file from the real ta file, fixing known issues."""
    src = Path(__file__).parent.parent / "test_data" / "CMIP7" / (
        "ta_tavg-p19-hxy-air_day_glb_g101_CNRM-ESM2-1e_piControl_r1i1p1f1_18500101-18511231.nc"
    )
    outdir = OUTDIR / "CMIP7"
    outdir.mkdir(parents=True, exist_ok=True)
    outname = "ta_tavg-p19-hxy-air_day_glb_g101_CNRM-ESM2-1e_piControl_r1i1p1f1_18500101-18500103.nc"

    with nc.Dataset(src, "r") as ds_in:
        with nc.Dataset(outdir / outname, "w", format="NETCDF4") as ds_out:
            # Subset dimensions: 3 timesteps, 3 plev, 4 lat, 5 lon
            nt, nplev, nlat, nlon = 3, 3, 4, 5

            # Create dimensions
            ds_out.createDimension("time", None)  # unlimited
            ds_out.createDimension("plev", nplev)
            ds_out.createDimension("lat", nlat)
            ds_out.createDimension("lon", nlon)
            ds_out.createDimension("axis_nbounds", 2)

            # Copy & subset coordinate variables
            lat_in = ds_in.variables["lat"]
            lat_out = ds_out.createVariable("lat", lat_in.dtype, ("lat",))
            lat_out[:] = lat_in[:nlat]
            for attr in lat_in.ncattrs():
                lat_out.setncattr(attr, lat_in.getncattr(attr))
            # Fix: set bounds attr to "lat_bnds" (standard name)
            lat_out.bounds = "lat_bnds"

            lon_in = ds_in.variables["lon"]
            lon_out = ds_out.createVariable("lon", lon_in.dtype, ("lon",))
            lon_out[:] = lon_in[:nlon]
            for attr in lon_in.ncattrs():
                lon_out.setncattr(attr, lon_in.getncattr(attr))
            lon_out.bounds = "lon_bnds"

            plev_in = ds_in.variables["plev"]
            plev_out = ds_out.createVariable("plev", plev_in.dtype, ("plev",))
            plev_out[:] = plev_in[:nplev]
            for attr in plev_in.ncattrs():
                plev_out.setncattr(attr, plev_in.getncattr(attr))

            time_in = ds_in.variables["time"]
            time_out = ds_out.createVariable("time", time_in.dtype, ("time",))
            time_out[:] = time_in[:nt]
            for attr in time_in.ncattrs():
                val = time_in.getncattr(attr)
                # FIX: calendar gregorian -> standard
                if attr == "calendar":
                    val = "standard"
                time_out.setncattr(attr, val)

            # Time bounds
            tb_in = ds_in.variables["time_bounds"]
            tb_out = ds_out.createVariable("time_bounds", tb_in.dtype, ("time", "axis_nbounds"))
            tb_out[:] = tb_in[:nt, :]
            for attr in tb_in.ncattrs():
                tb_out.setncattr(attr, tb_in.getncattr(attr))

            # FIX: lat/lon bounds with shape (n, 2) instead of (n, 4)
            # Compute proper bounds from coordinate values
            lat_vals = lat_out[:]
            lat_bnds_var = ds_out.createVariable("lat_bnds", "f8", ("lat", "axis_nbounds"))
            for i in range(nlat):
                if i == 0:
                    lower = lat_vals[i] - (lat_vals[1] - lat_vals[0]) / 2
                else:
                    lower = (lat_vals[i - 1] + lat_vals[i]) / 2
                if i == nlat - 1:
                    upper = lat_vals[i] + (lat_vals[i] - lat_vals[i - 1]) / 2
                else:
                    upper = (lat_vals[i] + lat_vals[i + 1]) / 2
                lat_bnds_var[i, :] = [lower, upper]

            lon_vals = lon_out[:]
            lon_bnds_var = ds_out.createVariable("lon_bnds", "f8", ("lon", "axis_nbounds"))
            for i in range(nlon):
                if i == 0:
                    lower = lon_vals[i] - (lon_vals[1] - lon_vals[0]) / 2
                else:
                    lower = (lon_vals[i - 1] + lon_vals[i]) / 2
                if i == nlon - 1:
                    upper = lon_vals[i] + (lon_vals[i] - lon_vals[i - 1]) / 2
                else:
                    upper = (lon_vals[i] + lon_vals[i + 1]) / 2
                lon_bnds_var[i, :] = [lower, upper]

            # Data variable (must set fill_value at creation time)
            ta_in = ds_in.variables["ta"]
            fill = ta_in.getncattr("_FillValue") if "_FillValue" in ta_in.ncattrs() else None
            ta_out = ds_out.createVariable(
                "ta", ta_in.dtype, ("time", "plev", "lat", "lon"),
                zlib=True, complevel=4, fill_value=fill,
            )
            ta_out[:] = ta_in[:nt, :nplev, :nlat, :nlon]
            for attr in ta_in.ncattrs():
                if attr not in ("_FillValue",):
                    ta_out.setncattr(attr, ta_in.getncattr(attr))

            # Copy global attributes with fixes
            for attr in ds_in.ncattrs():
                val = ds_in.getncattr(attr)
                if attr == "data_specs_version":
                    val = "MIP-DS7.1.0.0"
                elif attr == "license_id":
                    val = "CC-BY-4.0"
                # Remove the messy 'name' attribute (scratch path)
                elif attr == "name":
                    continue
                ds_out.setncattr(attr, val)

    print(f"  Created {outdir / outname}")


def make_cmip6_golden():
    """Create a golden CMIP6 file from the real psl file, fixing known issues."""
    src = Path(__file__).parent.parent / "test_data" / "CMIP6" / (
        "psl_Amon_CESM1-1-CAM5-CMIP5_dcppA-hindcast_s1980-r10i1p1f1_gn_198011-199012.nc"
    )
    outdir = OUTDIR / "CMIP6"
    outdir.mkdir(parents=True, exist_ok=True)
    outname = "psl_Amon_CESM1-1-CAM5-CMIP5_dcppA-hindcast_s1980-r10i1p1f1_gn_198011-198101.nc"

    with nc.Dataset(src, "r") as ds_in:
        with nc.Dataset(outdir / outname, "w", format="NETCDF4") as ds_out:
            # Subset: 3 timesteps, 4 lat, 5 lon
            nt, nlat, nlon = 3, 4, 5

            # Dimensions
            ds_out.createDimension("time", None)
            ds_out.createDimension("lat", nlat)
            ds_out.createDimension("lon", nlon)
            ds_out.createDimension("nbnd", 2)

            # Coordinates
            lat_in = ds_in.variables["lat"]
            lat_out = ds_out.createVariable("lat", lat_in.dtype, ("lat",))
            lat_out[:] = lat_in[:nlat]
            for attr in lat_in.ncattrs():
                lat_out.setncattr(attr, lat_in.getncattr(attr))

            lon_in = ds_in.variables["lon"]
            lon_out = ds_out.createVariable("lon", lon_in.dtype, ("lon",))
            lon_out[:] = lon_in[:nlon]
            for attr in lon_in.ncattrs():
                lon_out.setncattr(attr, lon_in.getncattr(attr))

            time_in = ds_in.variables["time"]
            time_out = ds_out.createVariable("time", time_in.dtype, ("time",))
            time_out[:] = time_in[:nt]
            for attr in time_in.ncattrs():
                time_out.setncattr(attr, time_in.getncattr(attr))

            # Time bounds
            if "time_bnds" in ds_in.variables:
                tb_in = ds_in.variables["time_bnds"]
                tb_out = ds_out.createVariable("time_bnds", tb_in.dtype, ("time", "nbnd"))
                tb_out[:] = tb_in[:nt, :]
                for attr in tb_in.ncattrs():
                    tb_out.setncattr(attr, tb_in.getncattr(attr))

            # Lat/lon bounds
            if "lat_bnds" in ds_in.variables:
                lb_in = ds_in.variables["lat_bnds"]
                lb_out = ds_out.createVariable("lat_bnds", lb_in.dtype, ("lat", "nbnd"))
                lb_out[:] = lb_in[:nlat, :]
                for attr in lb_in.ncattrs():
                    lb_out.setncattr(attr, lb_in.getncattr(attr))

            if "lon_bnds" in ds_in.variables:
                lb_in = ds_in.variables["lon_bnds"]
                lb_out = ds_out.createVariable("lon_bnds", lb_in.dtype, ("lon", "nbnd"))
                lb_out[:] = lb_in[:nlon, :]
                for attr in lb_in.ncattrs():
                    lb_out.setncattr(attr, lb_in.getncattr(attr))

            # Data variable (must set fill_value at creation time)
            psl_in = ds_in.variables["psl"]
            fill = psl_in.getncattr("_FillValue") if "_FillValue" in psl_in.ncattrs() else None
            psl_out = ds_out.createVariable(
                "psl", "f4", ("time", "lat", "lon"),
                zlib=True, complevel=4, fill_value=fill,
            )
            psl_out[:] = psl_in[:nt, :nlat, :nlon]
            for attr in psl_in.ncattrs():
                if attr not in ("_FillValue",):
                    psl_out.setncattr(attr, psl_in.getncattr(attr))

            # Copy global attributes with fixes
            for attr in ds_in.ncattrs():
                val = ds_in.getncattr(attr)
                # FIX: parent attributes - dcppA-hindcast parent is historical/CMIP
                if attr == "parent_activity_id":
                    val = "CMIP"
                elif attr == "parent_experiment_id":
                    val = "dcppA-assim"
                elif attr == "parent_mip_era":
                    val = "CMIP6"
                elif attr == "parent_source_id":
                    val = "CESM1-1-CAM5-CMIP5"
                elif attr == "parent_variant_label":
                    val = "r10i1p1f1"
                elif attr == "parent_time_units":
                    val = "days since 1850-01-01"
                ds_out.setncattr(attr, val)

    print(f"  Created {outdir / outname}")


def make_cordex_golden():
    """Create a golden CORDEX-CMIP6 file from the real tas file."""
    src = Path(__file__).parent.parent / "test_data" / "CORDEX-CMIP6" / (
        "tas_EUR-12_ERA5_evaluation_r1i1p1f1_GERICS_REMO2020-2-2_v1-r1_mon_197901-198812.nc"
    )
    outdir = OUTDIR / "CORDEX-CMIP6"
    outdir.mkdir(parents=True, exist_ok=True)
    outname = "tas_EUR-12_ERA5_evaluation_r1i1p1f1_GERICS_REMO2020-2-2_v1-r1_mon_197901-197903.nc"

    with nc.Dataset(src, "r") as ds_in:
        with nc.Dataset(outdir / outname, "w", format="NETCDF4_CLASSIC") as ds_out:
            # Subset: 3 timesteps, small spatial subset
            nt = 3

            # Copy all dimensions (CORDEX uses rotated grids)
            dim_sizes = {}
            for dname, dim in ds_in.dimensions.items():
                if dname == "time":
                    ds_out.createDimension(dname, None)
                    dim_sizes[dname] = nt
                elif dname in ("rlat", "y"):
                    size = min(len(dim), 4)
                    ds_out.createDimension(dname, size)
                    dim_sizes[dname] = size
                elif dname in ("rlon", "x"):
                    size = min(len(dim), 5)
                    ds_out.createDimension(dname, size)
                    dim_sizes[dname] = size
                else:
                    ds_out.createDimension(dname, len(dim))
                    dim_sizes[dname] = len(dim)

            # Copy all variables with subsetting
            for vname, var_in in ds_in.variables.items():
                dims = var_in.dimensions
                # Build slice for each dimension
                slices = []
                for d in dims:
                    slices.append(slice(0, dim_sizes.get(d, None)))

                kwargs = {}
                if var_in.filters():
                    kwargs["zlib"] = True
                    kwargs["complevel"] = 1
                    kwargs["shuffle"] = True

                var_out = ds_out.createVariable(
                    vname, var_in.dtype, dims, **kwargs,
                )
                var_out[tuple(slices)] = var_in[tuple(slices)]

                for attr in var_in.ncattrs():
                    if attr not in ("_FillValue",):
                        var_out.setncattr(attr, var_in.getncattr(attr))

            # Copy global attributes (CORDEX files were mostly OK)
            for attr in ds_in.ncattrs():
                ds_out.setncattr(attr, ds_in.getncattr(attr))

    print(f"  Created {outdir / outname}")


if __name__ == "__main__":
    print("Generating golden files...")
    print("\nCMIP7:")
    make_cmip7_golden()
    print("\nCMIP6:")
    make_cmip6_golden()
    print("\nCORDEX-CMIP6:")
    make_cordex_golden()
    print("\nDone!")
