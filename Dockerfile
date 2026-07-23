FROM python:3.12-slim

# System dependencies needed by netCDF4, HDF5, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libhdf5-dev \
    libnetcdf-dev \
    hdf5-tools \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install --no-cache-dir uv

# Create a workspace
WORKDIR /app

# Initialize a uv project and install tools + dependencies
RUN uv init --no-readme && \
    uv add esgvoc esgprep cc-plugin-wcrp cmip7-repack && \
    uv add pyudunits2

# Pre-download esgvoc controlled vocabularies (needed offline on cluster)
RUN uv run esgvoc use cmip7@latest && \
    uv run esgvoc use cmip6@latest && \
    uv run esgvoc use cordex-cmip6@latest && \
    uv run esgvoc use universe@latest

ENV USER=root

# Default entrypoint: drop into a shell with the venv activated
ENTRYPOINT ["uv", "run"]
CMD ["bash"]
