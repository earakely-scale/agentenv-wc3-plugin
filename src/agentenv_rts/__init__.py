"""Pieces any real-time strategy env and its agents can share, whatever the game: the `urn:rts:*` session contract
that lets a program (not only an LLM over MCP tools) play an env, the spectator timeline, live view and recording,
and the model helpers RTS agents use. Nothing here imports a game; `agentenv_wc3` is one game on top of it."""
