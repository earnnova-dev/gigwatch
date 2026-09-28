# GigWatch — self-hosted hosted instance (gigwatch serve)
#
# One-command, single-container self-hosting of the live GigWatch dashboard +
# JSON + RSS feed. Stdlib-only, no external services, no env vars required
# (config.json is baked in from config.example.json and can be overridden by
# mounting your own /app/config.json).
#
#   docker build -t gigwatch .
#   docker run -d -p 8765:8765 -v gigwatch-data:/app --name gigwatch gigwatch
#
# Endpoints (dashboard public; /api/jobs + /feed optional token via GIGWATCH_TOKEN):
#   http://localhost:8765/          live dashboard
#   http://localhost:8765/api/jobs  JSON matches
#   http://localhost:8765/feed      RSS feed
#   http://localhost:8765/health    liveness probe

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Non-root runtime user (container runs as 'gigwatch')
RUN groupadd -r gigwatch && useradd -r -g gigwatch -d /app -s /sbin/nologin gigwatch

WORKDIR /app

# Install the package (stdlib-only, no runtime deps)
COPY pyproject.toml README.md ./
COPY gigwatch ./gigwatch
RUN pip install --no-cache-dir .

# Ship a working default config; mount your own to override.
COPY config.example.json /app/config.json
RUN chmod a+rx /app && chown -R gigwatch:gigwatch /app

USER gigwatch

EXPOSE 8765

# The single, verified command that boots the hosted instance.
# GIGWATCH_TOKEN (optional) gates /api/jobs and /feed only (dashboard stays
# public so it can be shared as a link).
CMD ["sh", "-c", "exec gigwatch serve --host 0.0.0.0 --port 8765 ${GIGWATCH_TOKEN:+--token \"$GIGWATCH_TOKEN\"}"]
