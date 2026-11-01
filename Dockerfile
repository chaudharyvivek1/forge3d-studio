# forge3d-studio — one process, one SQLite file, one data volume.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STUDIO_DATA_DIR=/data \
    STUDIO_DEV_MODE=false

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 1000 studio && mkdir -p /data && chown -R studio /data
USER studio
VOLUME ["/data"]
EXPOSE 8000

# Studio executes nothing untrusted itself; generation happens in the engine,
# which the worker calls in-process. Keep this container off the public internet
# and put TLS in front of it.
CMD ["forge3d-studio", "--host", "0.0.0.0", "--port", "8000"]
