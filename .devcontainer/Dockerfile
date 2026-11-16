# Dev image for portfolio-financial-analysis (Python 3.12 + uv).
# Same base image and tooling the devcontainer used before (T-159): the uv devcontainer
# feature is replaced by the two steps below, so the image also builds with plain
# `docker build` and not only through the Dev Containers CLI.
# uv: copied from the official image (pinned; bump deliberately, uv.lock does not pin uv).
ARG UV_VERSION=0.12.1
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm
COPY --from=uv /uv /uvx /usr/local/bin/

# uv shell completion (the feature's `shellAutocompletion: true`).
RUN echo 'eval "$(uv generate-shell-completion bash)"' >> /etc/bash.bashrc \
    && echo 'eval "$(uvx --generate-shell-completion bash)"' >> /etc/bash.bashrc

# postCreateCommand (scripts/init.sh) still runs `uv sync`, the git hooks, gh and Claude Code.
