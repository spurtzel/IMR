# One Dockerfile, two targets:
#   trino-server : Trino 481 with this repo's server configuration
#   pipeline     : python:3.10-slim + pinned wheels + the repo; CMD runs the demo suite
#
#   docker build --target trino-server -t eimer-demo-trino    -f docker/Dockerfile .
#   docker build --target pipeline     -t eimer-demo-pipeline -f docker/Dockerfile .
#
ARG TRINO_IMAGE=trinodb/trino:481

FROM ${TRINO_IMAGE} AS trino-server
# Demo server configuration
COPY docker/trino-etc/ /etc/trino/

FROM python:3.10-slim AS pipeline
WORKDIR /eimer
# Pinned wheels
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . /eimer
# Demo connection defaults
ENV TRINO_SERVER=trino:8080 \
    TRINO_CATALOG=memory \
    TRINO_SCHEMA=default \
    TRINO_USER=eimer \
    MPLCONFIGDIR=/tmp/matplotlib \
    PYTHONUNBUFFERED=1
CMD ["bash", "docker/reproduce.sh"]
