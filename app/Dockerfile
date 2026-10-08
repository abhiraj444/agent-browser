# Cloud runner: headed Chromium on Xvfb + x11vnc + the agent server (noVNC served by the app, behind its token).
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium xvfb x11vnc novnc xdotool x11-utils fonts-noto fonts-noto-color-emoji ca-certificates curl \
      python3 python3-pip python3-venv && rm -rf /var/lib/apt/lists/*
RUN useradd -m agent
WORKDIR /opt/agentapp
COPY app/requirements.txt app/requirements.txt
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install -r app/requirements.txt
COPY agentmap/ agentmap/
COPY deploy/ deploy/
COPY PLAN.md .gitignore .dockerignore ./
COPY app/ app/
RUN ARCH=$(dpkg --print-architecture) && curl -sL -o /usr/local/bin/cloudflared \
      https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH && chmod +x /usr/local/bin/cloudflared
# uBlock Origin Lite (MV3 ad blocker) baked in and loaded on every start; more can be added from the dashboard
RUN mkdir -p /opt/agentapp/extensions/ubol && curl -sL -A 'Mozilla/5.0' -o /tmp/ubol.crx \
      'https://clients2.google.com/service/update2/crx?response=redirect&prodversion=140.0&acceptformat=crx2,crx3&x=id%3Dddkjiahejlhfcafbddmgiahcphecmpfh%26uc' \
    && python3 -c "import io,zipfile;d=open('/tmp/ubol.crx','rb').read();zipfile.ZipFile(io.BytesIO(d[d.find(b'PK\\x03\\x04'):])).extractall('/opt/agentapp/extensions/ubol')" \
    && rm -rf /opt/agentapp/extensions/ubol/_metadata /tmp/ubol.crx && chmod -R a+rX /opt/agentapp/extensions || echo 'uBOL download failed; continuing without it'
USER agent
# persistent profile + token live in a volume so cookies/clearances survive restarts (same IP = same host)
VOLUME /home/agent/.agentapp
ENV PATH=/opt/venv/bin:$PATH NO_PROXY=127.0.0.1,localhost PORT=8080
EXPOSE 8080
CMD ["python3", "app/main.py", "--tunnel"]
