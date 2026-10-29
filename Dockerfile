# --- Build stage: compile native extensions ---
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    libhdf5-dev \
    libnetcdf-dev \
    libudunits2-dev \
    libudunits2-data \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

ENV UDUNITS2_XML_PATH=/usr/share/xml/udunits/udunits2.xml

RUN pip install --no-cache-dir uv

WORKDIR /app

RUN uv init --no-readme && \
    uv add esgvoc esgprep cc-plugin-wcrp cmip7-repack esgf-qa && \
    uv add pyudunits2

# Pre-download esgvoc controlled vocabularies into a fixed path
ENV ESGVOC_HOME=/app/esgvoc_data

RUN uv run esgvoc use universe@latest && \
    uv run esgvoc use cmip6@latest && \
    uv run esgvoc use cmip6plus@latest && \
    uv run esgvoc use cmip7@latest && \
    uv run esgvoc use cordex-cmip6@latest

# --- Runtime stage: only what's needed to run ---
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libhdf5-310 \
    libhdf5-hl-310 \
    libnetcdf22 \
    libudunits2-0 \
    libudunits2-data \
    hdf5-tools \
    man-db \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv
COPY --from=builder /app /app

WORKDIR /app

ENV USER=root
ENV ESGVOC_HOME=/app/esgvoc_data
ENV UDUNITS2_XML_PATH=/usr/share/xml/udunits/udunits2.xml

ENV PATH="/app/.venv/bin:${PATH}"

ENTRYPOINT []
CMD ["/bin/bash"]
