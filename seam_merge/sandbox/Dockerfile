# SEAM sandbox image: runs worker agents (Claude Code) and every test run.
#   seam image build      (ships inside the package; no build context is sent)
FROM node:22-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && ln -s /usr/bin/python3 /usr/local/bin/python \
 && pip3 install --no-cache-dir --break-system-packages pytest

# Pinned so a release builds the same image; override with
# `seam image build --claude-code-version X`.
ARG CLAUDE_CODE_VERSION=2.1.312
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

USER node
WORKDIR /work
