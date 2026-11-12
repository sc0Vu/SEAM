# SEAM sandbox image: runs worker agents (Claude Code) and every test run.
#   docker build -t seam-sandbox sandbox/
FROM node:22-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && ln -s /usr/bin/python3 /usr/local/bin/python \
 && pip3 install --no-cache-dir --break-system-packages pytest

ARG CLAUDE_CODE_VERSION=latest
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

USER node
WORKDIR /work
