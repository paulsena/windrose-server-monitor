# Repository Guidelines

## Project Structure & Module Organization

This is a small, dependency-free Python utility for monitoring a Windrose dedicated server log.

- `windrose_monitor.py` contains log parsing, roster state, Discord webhook delivery, the HTTP dashboard, and the JSON API.
- `README.md` is the user-facing setup and operations guide.
- `GEMINI.md` contains existing agent notes; keep it consistent when behavior or commands change.
- `assets/` stores visual assets used for documentation or theming.
- `R5.log` is a local/sample server log. Do not rely on machine-specific paths when adding examples.

There is no test directory yet. If tests are added, place them under `tests/` with small, anonymized fixtures.

## Build, Test, and Development Commands

No install step is required; the project uses only the Python standard library.

```bash
python windrose_monitor.py --help
```
Shows all supported CLI options.

```bash
python windrose_monitor.py --log R5.log --host 127.0.0.1 --port 8080
```
Runs the dashboard locally against the sample or active log.

```bash
python -m py_compile windrose_monitor.py
```
Performs a quick syntax/import smoke check.

## Coding Style & Naming Conventions

Use Python 3.8+ compatible standard-library code only. Follow the existing style: 4-space indentation, practical type hints, `snake_case` for functions and variables, and `PascalCase` for classes. Keep constants uppercase, such as `ROW_RE`.

Prefer small helpers for parsing and formatting. Keep comments brief and focused on log-format edge cases or thread interactions.

## Testing Guidelines

The repository does not define a formal test suite. For changes today, run `python -m py_compile windrose_monitor.py` and manually exercise the affected path with `R5.log` when possible.

When adding tests, use `unittest` unless a dependency decision is made explicitly. Name files `tests/test_*.py`, and cover parser behavior, roster transitions, API responses, and webhook error handling.

## Commit & Pull Request Guidelines

Recent commits use concise, imperative summaries, for example `Add --webhook CLI arg; remove --no-replay`. Follow that style: start with a verb, describe the behavior changed, and keep the subject focused.

Pull requests should include a short description, manual verification commands, and screenshots or notes for dashboard UI changes. Link related issues and call out CLI, webhook, or log parsing changes.

## Security & Configuration Tips

Do not commit real Discord webhook URLs, private server logs, or player-identifying data. Prefer environment variables such as `DISCORD_WEBHOOK_URL` for secrets, and use sanitized log snippets for bug reports and tests.
