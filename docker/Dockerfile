# One Dockerfile, two targets:
#   trino-server : the pinned Trino (481) with this repo's server configuration
#   pipeline     : python:3.10-slim + pinned wheels + the repo; CMD runs the demo suite
#
#   docker build --target trino-server -t eimer-demo-trino    -f docker/Dockerfile .
#   docker build --target pipeline     -t eimer-demo-pipeline -f docker/Dockerfile .
#
# The Trino version is pinned for reproducibility; override only if you know why:
ARG TRINO_IMAGE=trinodb/trino:481

FROM ${TRINO_IMAGE} AS trino-server
# The exact server configuration the results were produced under (single node,
# memory catalog, spill enabled, join reordering + intra-query parallelism disabled).
COPY docker/trino-etc/ /etc/trino/

FROM python:3.10-slim AS pipeline
WORKDIR /eimer
# Pinned wheels first (build-cache friendly). The pins matter: the synthetic data
# generator is bit-reproducible only under the pinned numpy/pandas/pyarrow.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . /eimer
# Demo connection defaults (the run_demo.sh network aliases the server as "trino")
ENV TRINO_SERVER=trino:8080 \
    TRINO_CATALOG=memory \
    TRINO_SCHEMA=default \
    TRINO_USER=eimer \
    MPLCONFIGDIR=/tmp/matplotlib \
    PYTHONUNBUFFERED=1
CMD ["bash", "docker/reproduce.sh"]
