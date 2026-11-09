# AVRONE server image. Least privilege by default:
#   - runs as an unprivileged user (uid/gid 10001; override with --build-arg or compose `user:`)
#   - sandbox containers get no network unless AVRONE_ALLOW_NETWORK=true
#   - HEALTHCHECK hits /api/status (no auth needed, no side effects)
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends docker.io git curl traceroute mtr-tiny iputils-ping dnsutils && rm -rf /var/lib/apt/lists/*
ARG AVRONE_UID=10001
ARG AVRONE_GID=10001
RUN groupadd --system --gid "${AVRONE_GID}" avrone \
 && useradd --system --uid "${AVRONE_UID}" --gid avrone --home-dir /home/avrone --create-home \
            --shell /usr/sbin/nologin avrone \
 && mkdir -p /srv/workspace /srv/data \
 && chown avrone:avrone /srv/workspace /srv/data
WORKDIR /app
COPY pyproject.toml ./
COPY avrone ./avrone
COPY Verify.sh ./
COPY avrone-a12lr/tests ./avrone-a12lr/tests
RUN pip install --no-cache-dir . && rm -rf /root/.cache \
 && chown -R avrone:avrone /app
ENV AVRONE_HOST=0.0.0.0 AVRONE_PORT=8710 AVRONE_WORKSPACE=/srv/workspace AVRONE_DATA_DIR=/srv/data \
    AVRONE_ALLOW_NETWORK=false HOME=/home/avrone
USER avrone
EXPOSE 8710
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('AVRONE_PORT', '8710') + '/api/status', timeout=4)" || exit 1
CMD ["avrone"]
