#!/usr/bin/env bash
# Called by the post-receive hook from the checked-out tree: build image, swap container, print the dashboard URL.
set -euo pipefail
echo "$(date -Is) deploying $(git --git-dir=$HOME/agent-browser.git log -1 --pretty='%h %s' main)"
sudo docker build -q -t agent-browser:latest -f app/Dockerfile . >/dev/null
sudo docker rm -f agent-browser >/dev/null 2>&1 || true
sudo docker run -d --name agent-browser --restart unless-stopped --shm-size 1g \
  -p 127.0.0.1:8080:8080 -v agentapp:/home/agent/.agentapp agent-browser:latest >/dev/null
sudo docker image prune -f >/dev/null
# mirror the deployed code to the user's private GitHub repo (token/repo from dashboard Settings); never fails the deploy
mirror_github() {
  MSG=$(git --git-dir=$HOME/agent-browser.git log -1 --pretty='%h %s' main | tr -cd 'A-Za-z0-9 ._,:/+()-')
  sudo docker exec agent-browser sh -c "set -a; [ -f ~/.agentapp/env ] && . ~/.agentapp/env; set +a; python3 app/ghsync.py -m '$MSG'" \
    2>&1 | tail -1 | sed 's/^/github: /' || echo "github mirror skipped"
}
for i in $(seq 1 60); do
  U=$(sudo docker logs agent-browser 2>&1 | grep -o 'READY .*' | tail -1 || true)
  [ -n "$U" ] && { echo "$U" | tee "$HOME/agent-browser.url"; mirror_github; exit 0; }
  sleep 3
done
echo "container started; no READY line yet:"; sudo docker logs --tail 30 agent-browser
