# portfolio-finantial-analysis

## Development

This project uses [uv](https://docs.astral.sh/uv/) for environment management and
[pre-commit](https://pre-commit.com/) (Ruff + mypy + Commitizen) for code quality.

### Quick start

```bash
uv sync --group dev                 # create .venv from uv.lock
uv run pre-commit install --install-hooks
```

Opening the repo in the provided **Dev Container** (`.devcontainer/`) runs both
steps automatically and pins the exact toolchain (Python 3.12 + uv).

### Common tasks

```bash
uv run ruff check --config .code_quality/ruff.toml .      # lint
uv run ruff format --config .code_quality/ruff.toml .     # format
uv run mypy --config-file .code_quality/mypy.ini .        # type-check
uv run pre-commit run --all-files                         # everything
```

Commits follow [Conventional Commits](https://www.conventionalcommits.org/);
`cz commit` (via `uv run cz commit`) helps compose them.
