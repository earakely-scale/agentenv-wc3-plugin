# A stand-in for wc3env's worker image (its `environment` target) without Wine or the game: the same layout, with
# the env's worker on wc3env's fake game. It tests this plugin's image and a whole agent-env run on any Linux
# Docker host. Build it with scripts/standin.sh.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates python3 xvfb x11-utils \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 worker && useradd --uid 10001 --gid 10001 --create-home worker \
 && mkdir -p /opt/game /opt/worker && chown 10001:10001 /opt/game \
 && printf '#!/bin/sh\nexit 0\n' > /usr/local/bin/wineserver && chmod +x /usr/local/bin/wineserver
COPY with-display.sh /opt/worker/
COPY wc3env/ /opt/host/wc3env/
ENV DISPLAY=:99 WC3_WORKER_CMD="/opt/agentenv/bin/python -m agentenv_wc3.worker --fake"
USER 10001:10001
