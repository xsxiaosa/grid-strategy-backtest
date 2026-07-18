# Repository Guidelines

## Project Structure & Module Organization

Core Python code lives in `grid_backtest/`. Keep strategy execution in `engine.py`, configuration models in `config.py`, market retrieval in `market_data.py`, persistence in `storage.py`, orchestration in `service.py`, and HTTP handling in `web_server.py`. The entry point is `start.py` for the local application; parameter optimization is provided by the web page at `/optimizer.html`. Browser assets are plain HTML, CSS, and JavaScript under `web/`. Tests live in `tests/` and mirror the modules they cover. Runtime JSON files belong under `data/`; avoid committing incidental reports or caches unless they are intentional fixtures.

## Build, Test, and Development Commands

The project requires Python 3.11+ and only the standard library; do not add npm or Python packages without a clear need.

- `python start.py` — start the local server at `http://127.0.0.1:8765`.
- `python -m unittest discover -s tests -v` — run the complete test suite.
- `python -m unittest tests.test_engine -v` — run one focused test module.

No separate build step is required.

## Coding Style & Naming Conventions

Use four-space indentation and standard Python conventions: `snake_case` for functions and variables, `PascalCase` for classes, and `UPPER_CASE` for constants. Keep functions focused and use type hints where they clarify data contracts. All added code must include useful comments. Document every method with detailed XML-style documentation that explains its purpose; for Python, use an equivalent structured docstring. Keep browser code dependency-free and follow the existing `camelCase` JavaScript style.

## Testing Guidelines

Use `unittest`; name files `test_<area>.py` and methods `test_<behavior>`. Add regression coverage for strategy math, persistence, optimization, and HTTP changes. Tests must not depend on live Yahoo Finance access; use deterministic inputs or repository fixtures. Run the full suite before submitting.

## Commit & Pull Request Guidelines

Current history uses concise Chinese commit subjects (for example, `初始化网格策略回测工具`). Continue with short, imperative, single-purpose subjects. Pull requests should explain behavior changes, list validation commands, link related issues, and call out data-format changes. Include screenshots for changes under `web/`. Do not commit secrets, personal strategy data, `__pycache__/`, or generated optimization/report output.

## Agent-Specific Instructions

Agents must communicate in Chinese. For non-Windows administration automation, use Python scripts rather than PowerShell scripts. Preserve the standard-library-only architecture unless the task explicitly authorizes a dependency.
