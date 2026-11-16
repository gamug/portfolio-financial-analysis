# Dev image for portfolio-financial-analysis (Python 3.12 + uv), used by .devcontainer/devcontainer.json
# and buildable on its own with `docker build .` from the repo root (so the build context is the root and
# .dockerignore keeps it small; nothing is COPYed into the image, the code arrives through the workspace mount).

# uv is copied from the official image. It is pinned here on purpose (uv.lock does not pin uv): bump
# UV_VERSION deliberately, in its own commit. The base image tag below floats within Python 3.12/bookworm.
ARG UV_VERSION=0.12.1
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm
COPY --from=uv /uv /uvx /usr/local/bin/

# uv shell completion (the previous uv feature's `shellAutocompletion: true`).
RUN echo 'eval "$(uv generate-shell-completion bash)"' >> /etc/bash.bashrc \
    && echo 'eval "$(uvx --generate-shell-completion bash)"' >> /etc/bash.bashrc

# `postCreateCommand` (scripts/init.sh) still runs `uv sync`, the git hooks, gh and Claude Code.
