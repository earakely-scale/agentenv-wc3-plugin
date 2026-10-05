# The Warcraft III env: this plugin's MCP server on top of your own wc3env worker image, which holds Wine, Windows
# Python with wc3env, and your game files. No image is distributed: `agent-env wc3 setup` builds this one locally
# from yours, and it must stay private. The activation files never enter it: the wc3_match step sends them at run
# time (or mount them at /run/wc3-license, as wc3env's own image takes them).
#   Build: agent-env wc3 setup   (docker build --platform linux/amd64 --build-arg BASE=wc3-worker:local .)
ARG BASE=wc3-worker:local
FROM ${BASE}

USER root
RUN apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends python3-venv ffmpeg \
 && rm -rf /var/lib/apt/lists/*
# The server runs in the image's Linux Python; the game worker it starts runs in Wine's Windows Python, which
# already has wc3env. The Linux side reads wc3env's prepared game data from the sources the image keeps in /opt/host.
# Pillow and ffmpeg render the spectator recording (agentenv_rts.recording).
RUN python3 -m venv /opt/agentenv \
 && /opt/agentenv/bin/pip install --no-cache-dir "agentenv-framework-protocol>=0.1.275,<0.2" "mcp>=1.25,<2" \
    "pydantic>=2,<3" "pillow>=10.1" \
 && echo /opt/host > "$(/opt/agentenv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/wc3env-host.pth"
COPY pyproject.toml README.md LICENSE /tmp/plugin/
COPY src/ /tmp/plugin/src/
COPY docker/with-display.sh /opt/agentenv/with-display.sh
RUN /opt/agentenv/bin/pip install --no-cache-dir --no-deps /tmp/plugin && rm -rf /tmp/plugin \
 && mkdir -p /tmp/wc3-sessions && chown 10001:10001 /tmp/wc3-sessions
USER 10001:10001

# As wc3env's docker/entrypoint.sh sets them for the game under Wine.
ENV WC3_GAME_DIR='C:\wc3' WC3_USER_DIR='C:\scratch\Warcraft III' WC3_OUTPUT_DIR='Z:\tmp\wc3-sessions' \
    MCP_HOST=0.0.0.0 MCP_PORT=18765 WC3_SCREEN=1920x1080x24
EXPOSE 18765
# with-display.sh starts Xvfb (WC3_SCREEN), runs the server, and stops Wine and the display when it exits.
ENTRYPOINT ["bash", "/opt/agentenv/with-display.sh", "/opt/agentenv/bin/python", "-m", "agentenv_wc3.server"]
CMD []
