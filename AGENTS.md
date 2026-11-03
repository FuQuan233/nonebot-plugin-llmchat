# Repository Guidelines

## Project Structure & Module Organization

- `nonebot_plugin_llmchat/` contains the NoneBot2 plugin for OneBot V11. `__init__.py` handles registration and lifecycle; `conversation.py` handles LLM requests and tool loops; `dispatcher.py` queues messages per conversation.
- `memory.py`, `state.py`, and `context_manager.py` manage conversation context. `models.py`, `persistence.py`, and `migrations/` provide datastore persistence.
- `tests/` contains regression tests and `bootstrap.py`, which loads submodules without plugin registration. `img/` stores README screenshots; `.github/workflows/` defines CI and publishing.

## Build, Test, and Development Commands

Use Python 3.10+ and uv with the repository's `.venv`. Run from the repository root:

```sh
uv sync --locked                     # Install locked runtime and development dependencies
uv run --locked pre-commit install    # Enable lint, format, and commit-message hooks
uv run --locked ruff check .          # Check lint rules and import ordering
uv run --locked ruff format --check . # Check formatting; omit --check to format
uv run --locked pyright              # Check types against the project environment
uv run --locked python -m unittest discover -s tests -v # Run regression tests
uv build                             # Build wheel and source distributions
```

For integration testing, install the checkout with `uv pip install --python <bot-venv-python> -e .`, enable `nonebot_plugin_llmchat`, and run the host bot using `nb run` (requires nb-cli). CI runs the same checks from the local virtual environment.

## Coding Style & Naming Conventions

Use four spaces for Python, two for other text files, UTF-8 without BOM, and LF endings. Ruff targets Python 3.10 with a 130-character line limit and sorted imports. Use `snake_case` for modules/functions, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants. Keep registration, conversation logic, transport adapters, and persistence separate; prefer small changes and shared helpers over speculative abstractions.

## Testing Guidelines

Use `unittest.TestCase` and `unittest.IsolatedAsyncioTestCase`. Name files `tests/test_<feature>.py` and methods `test_<behavior>`. Import `tests.bootstrap` before plugin submodules when needed. Use fakes and temporary storage to isolate external APIs, MCP sessions, and databases. Add regression coverage for behavior changes; no numeric coverage threshold is configured.

## Commit & Pull Request Guidelines

Recent commits use Gitmoji prefixes with short Chinese summaries, sometimes referencing issues. Follow the prefix convention, for example `🐛 Fix MCP session cleanup`. Keep commits focused. PRs should describe the problem, behavior change, linked issues, and validation commands/results. Include configuration or migration notes when relevant, and screenshots for visible output changes. Update `README.md` for user-facing changes.

## Security & Configuration

Keep API keys and MCP credentials in the host bot's untracked `.env`. Configure `NICKNAME` and `LLMCHAT__API_PRESETS` using README examples. Never commit credentials, conversation databases, or runtime sticker data.
