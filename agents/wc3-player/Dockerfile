# The wc3-macro-micro agent: wc3env's own wc3agent (macro + micro) playing the env through its urn:rts session
# (agent.py, remote.py). wc3env and wc3agent come from wc3env at a pinned commit; they need no game files here.
# Build: agent-env wc3 setup --agent, or docker build -t a2a-agent-wc3-macro-micro -f agents/wc3-player/Dockerfile .
FROM python:3.12-slim
ARG WC3ENV_COMMIT=eb660aa558fb6e5c639a1ff404082f7dc0ee483e
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/wc3env/src:/opt/wc3env/wc3agent/src:/app
RUN pip install "agentenv-framework-protocol[agent]==0.1.282" \
 && python -c "import io, tarfile, urllib.request; tarfile.open(fileobj=io.BytesIO(urllib.request.urlopen( \
      'https://codeload.github.com/pwang724/wc3env/tar.gz/${WC3ENV_COMMIT}', timeout=300).read())).extractall('/opt')" \
 && mv "/opt/wc3env-${WC3ENV_COMMIT}" /opt/wc3env \
 && rm -rf /opt/wc3env/tests /opt/wc3env/wc3hook /opt/wc3env/docs /opt/wc3env/wc3agent/tests \
 && python -c "import wc3agent.play, wc3agent.micro.agent" \
 && useradd --uid 10001 --create-home player
COPY src/agentenv_rts /app/agentenv_rts
COPY agents/wc3-player/agent.py agents/wc3-player/remote.py /app/
USER player
WORKDIR /home/player
EXPOSE 8000
CMD ["python", "/app/agent.py"]
