# The wc3-llm agent: any chat model plays through the env's MCP tools in a plain tool loop (agent.py). No wc3env, no
# game files; the model comes from agent-env's model endpoint.
# Build: agent-env wc3 setup --agent, or docker build -t a2a-agent-wc3-llm -f agents/wc3-llm/Dockerfile .
FROM python:3.12-slim
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
RUN pip install "agentenv-framework-protocol[agent]==0.1.282" "mcp>=1.25,<2" "httpx>=0.27,<1" \
 && useradd --uid 10001 --create-home player
COPY src/agentenv_rts /app/agentenv_rts
COPY agents/wc3-llm/agent.py /app/
USER player
WORKDIR /home/player
EXPOSE 8000
CMD ["python", "/app/agent.py"]
