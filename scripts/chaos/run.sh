#!/bin/bash
# scripts/chaos/run.sh TASK ACTION: run TASK of this folder's bundle on this machine's Docker; once its game is 10
# game seconds in, break it. ACTION: agent kills red's agent container; game kills the game inside the env; broadcast
# kills the streamer. Then it reports how the run ended. The tasks are broadcast-smoke with short stall timeouts
# (lockstep 30 s, broadcast 90 s) and play steps that time out after 300 s, so each run ends within minutes.
task=$1; action=$2; here=$(cd "$(dirname "$0")" && pwd); log=${TMPDIR:-/tmp}/chaos-$task.log
agent-env run "$here" --task "$task" > "$log" 2>&1 &
run=$!
for _ in $(seq 1 240); do
  port=$(docker ps --format '{{.Image}} {{.Ports}} {{.CreatedAt}}' | grep mcp-server-wc3 | sort -k3 -r \
         | grep -o '127.0.0.1:[0-9]*->18765' | head -1 | cut -d- -f1)
  t=$( [ -n "$port" ] && curl -s -m 3 "http://$port/agentenv/ext/match" \
       | python3 -c 'import json,sys; print((json.load(sys.stdin) or {}).get("progress", [{}])[0].get("value") or 0)' 2>/dev/null)
  python3 -c "import sys; sys.exit(0 if float('${t:-0}') >= 10 else 1)" && break
  sleep 3
done
echo "game at $port, t=$t: breaking $action"
case $action in
  agent)
    for c in $(docker ps --format '{{.ID}} {{.Image}}' | grep wc3-scripted | cut -d' ' -f1); do
      docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$c" | grep -q '^SCRIPT=attack' \
        && docker kill "$c" > /dev/null && echo "killed red's agent $c"
    done ;;
  game)
    env=$(docker ps --format '{{.ID}} {{.Ports}}' | grep "$port->" | cut -d' ' -f1)
    docker exec "$env" bash -c 'pkill -9 -f "Warcraft III"; pkill -9 -f wineserver'; echo "killed the game in $env" ;;
  broadcast)
    for c in $(docker ps --format '{{.ID}} {{.Names}}' | grep game-broadcast | cut -d' ' -f1); do
      docker kill "$c" > /dev/null && echo "killed the streamer $c"
    done ;;
  *) echo "ACTION is agent, game or broadcast" >&2; kill $run; exit 2 ;;
esac
wait $run
echo "run exit $? (log: $log)"
grep -E "Step .* failed|failed at|scored|passed in" "$log" | tail -6
