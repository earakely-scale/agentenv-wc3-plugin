# The wc3-scripted agent: wc3agent's scripted opponent (attack, raid or idle) playing its player slot through the env's
# urn:rts session (agent.py). No model, no wc3env, no game files.
# Build: agent-env wc3 setup --agent, or docker build -t a2a-agent-wc3-scripted -f agents/wc3-scripted/Dockerfile .
FROM python:3.12-slim
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
RUN pip install "agentenv-framework-protocol[agent]==0.1.282" \
 && useradd --uid 10001 --create-home player
COPY src/agentenv_rts /app/agentenv_rts
COPY agents/wc3-scripted/agent.py /app/
USER player
WORKDIR /home/player
EXPOSE 8000
CMD ["python", "/app/agent.py"]
