#!/usr/bin/env bash
# One-time server setup (Ubuntu 24.04): Docker, swap, a bare git repo whose post-receive hook rebuilds + restarts the app.
# Afterwards deploying is just:  git push oracle main
set -euo pipefail
if ! command -v docker >/dev/null; then sudo apt-get update -y && sudo apt-get install -y docker.io git; sudo systemctl enable --now docker; fi
command -v git >/dev/null || sudo apt-get install -y git
if ! swapon --show | grep -q swapfile; then
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi
sudo docker volume create agentapp >/dev/null
sudo docker run --rm -v agentapp:/v debian:trixie-slim chown -R 1000:1000 /v
[ -d ~/agent-browser.git ] || git init -q --bare -b main ~/agent-browser.git
mkdir -p ~/agent-browser
cat > ~/agent-browser.git/hooks/post-receive <<'H'
#!/usr/bin/env bash
# on every push to main: check out the code and rebuild/restart the container (output streams back to the pusher)
while read old new ref; do
  [ "$ref" = "refs/heads/main" ] || continue
  git --work-tree="$HOME/agent-browser" --git-dir="$HOME/agent-browser.git" checkout -q -f main
  cd "$HOME/agent-browser" && bash deploy/autodeploy_local.sh 2>&1 | tee -a "$HOME/deploy.log"
done
H
chmod +x ~/agent-browser.git/hooks/post-receive
echo "server ready: push to ubuntu@<ip>:agent-browser.git"
