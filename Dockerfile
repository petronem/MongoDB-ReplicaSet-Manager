FROM python:3.13-slim

LABEL org.opencontainers.image.title="mongo-replica-ctrl" \
	  org.opencontainers.image.description="MongoDB ReplicaSet Manager for Docker Swarm w/TLS, Secrets & External Access" \
	  org.opencontainers.image.source="https://github.com/petronem/MongoDB-ReplicaSet-Manager" \
	  org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TERM=xterm-256color

# Install runtime dependencies with conservative pins
# - PyMongo 4.15+ supports MongoDB 7.x/8.x and Python 3.13
# - Pin docker/backoff to stable major versions to avoid surprise breaks
RUN python -m pip install --upgrade --no-cache-dir pip \
 && pip install --no-cache-dir \
	"pymongo>=4.15,<5" \
	"docker>=7,<9" \
	"backoff>=2.2,<3"

RUN mkdir /src
WORKDIR /src

COPY src/db-replica_ctrl.py /src/db-replica_ctrl.py

CMD ["python", "/src/db-replica_ctrl.py"]
