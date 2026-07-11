# Codex workflow

This directory contains reusable task prompts for OpenAI Codex. The repository-wide rules live in `/AGENTS.md`.

These files mirror the intent of `.claude/agents/` without changing the Claude Code setup. They are prompt templates and checklists; Codex does not treat them as independently running agents automatically.

## Suggested use

1. Start with `prompts/explore.md` when the relevant code or configuration is unclear.
2. Use `prompts/config-guard.md` after changes to Python, YAML, configuration loading, demand equations, electrification, or scenario handling.
3. Use `prompts/test-runner.md` to validate code changes.
4. Use `prompts/gap-flagger.md` for research notes, reports, and thesis text.
5. Use `prompts/source-checker.md` to verify claims flagged by the gap review.

## Example task requests

- `Follow .codex/prompts/explore.md and locate where electrification is calculated.`
- `Follow .codex/prompts/config-guard.md and review my current diff.`
- `Follow .codex/prompts/test-runner.md for tests/test_demand_model.py.`
- `Follow .codex/prompts/gap-flagger.md and review docs/air_pollution.md.`
- `Follow .codex/prompts/source-checker.md and verify this claim: ...`

## Maintenance

When a shared modelling rule changes, update both `/AGENTS.md` and the corresponding Claude agent so the two toolchains do not drift.
