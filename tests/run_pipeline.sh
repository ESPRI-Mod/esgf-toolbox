#!/usr/bin/env bash
# Run the full ESGF pipeline (QA/QC + esgdrs + esgmapfile) on golden test files.
# Expects golden files mounted at /data/{CMIP7,CMIP6,CORDEX-CMIP6}/
#
# Usage (local):
#   docker run --rm -v $PWD/tests/golden:/data esgf-toolbox bash /data/../tests/run_pipeline.sh
#
# Usage (cluster):
#   pcocc-rs run --mount src=/path/to/golden,dst=/data \
#     --env PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
#     esgf-toolbox -- bash /data/../tests/run_pipeline.sh
#
# Or copy this script next to golden files and run:
#   bash /path/to/run_pipeline.sh
#
# Options:
#   --offline   Skip CORDEX QA/QC (it requires network access to download CMOR tables)

set -e

OFFLINE=false
for arg in "$@"; do
    case $arg in
        --offline) OFFLINE=true ;;
    esac
done

DATA=${DATA:-/data}
failures=0

mkdir -p /tmp/drs /tmp/mapfiles

for proj_dir in "$DATA"/*/; do
    proj=$(basename "$proj_dir")
    case $proj in
        CMIP7)        p=cmip7;        cc=wcrp_cmip7;        drs_top=MIP-DRS7 ;;
        CMIP6)        p=cmip6;        cc=wcrp_cmip6;        drs_top=CMIP6 ;;
        CORDEX-CMIP6) p=cordex-cmip6; cc=wcrp_cordex_cmip6; drs_top=CORDEX-CMIP6 ;;
        *) echo "Skipping unknown project: $proj"; continue ;;
    esac

    echo ""
    echo "========== $proj =========="

    echo "--- QA/QC ---"
    if [ "$OFFLINE" = true ] && [ "$proj" = "CORDEX-CMIP6" ]; then
        echo "SKIPPED (--offline: CORDEX checker requires network for CMOR tables)"
    else
        compliance-checker -t "$cc" "$proj_dir"/*.nc || true
    fi

    echo "--- esgdrs ---"
    cp -r "$proj_dir" "/tmp/${proj}_in"
    if esgdrs make upgrade -p "$p" --root /tmp/drs "/tmp/${proj}_in/"; then
        echo "--- esgmapfile ---"
        esgmapfile make -p "$p" --outdir /tmp/mapfiles --no-checksum --directory "/tmp/drs/$drs_top"
    else
        echo "FAILED: esgdrs for $proj"
        failures=$((failures + 1))
    fi
done

echo ""
echo "========== Results =========="
if ls /tmp/mapfiles/*.map 1>/dev/null 2>&1; then
    echo "Generated mapfiles:"
    for f in /tmp/mapfiles/*.map; do
        echo "  $(basename "$f")"
    done
else
    echo "No mapfiles generated."
fi

if [ $failures -gt 0 ]; then
    echo "$failures project(s) failed."
    exit 1
else
    echo "All projects passed."
fi
