# Codex repository instructions

## Project scope

This repository contains the TRA420 Western Balkans energy-demand and climate-impact modelling application. Preserve its config-driven, region-agnostic architecture.

## Operating principles

- Read relevant files before editing. Use repository search and Git history when the reason for existing behaviour is unclear.
- Make the smallest change that satisfies the task.
- Do not silently invent, interpolate, or substitute missing research data. Flag gaps explicitly in code, logs, configuration, or documentation.
- Keep modelling logic separate from thesis interpretation and user-interface presentation.
- Do not change `.claude/agents/`; those files are maintained for Claude Code.

## Mandatory architecture checks

For changes to Python, YAML, configuration loading, demand equations, electrification, scenario data, or model documentation, apply the following checks:

1. **Explicit configuration**
   - Modelling scripts must require `--config` unless the file is clearly repository tooling rather than model execution.
   - Do not add hidden fallback values for required model inputs.

2. **Region-agnostic model logic**
   - Python model behaviour must not branch on hardcoded country or region names.
   - Country names may appear in tests, user-facing labels, documentation, and diagnostics when they do not control model logic.
   - Region-specific values belong in YAML or data files.

3. **Scenario identity**
   - When combining Integrated Assessment Model data, identify scenarios by `(model, scenario)` pairs, not scenario name alone.

4. **Double-counting control**
   - When modifying demand, electrification, income elasticity, price elasticity, or total final energy calculations, identify whether the same effect is already represented elsewhere.
   - Document the separation of effects before accepting a new term.

5. **Gap flagging**
   - Missing, proxied, transferred, interpolated, or assumed values must be visible and traceable.
   - Prefer an explicit warning, status field, provenance note, or validation error over silent completion.

## Validation

- Run the narrowest relevant tests first.
- Use `pytest --basetemp=./.pytest_tmp <args>` rather than bare `pytest`.
- For Python syntax checks, use `python -m py_compile` on changed files when appropriate.
- Report environment failures separately from model or logic failures.

## Research and documentation checks

For Markdown reports, thesis material, and research notes:

- Source externally derived numeric claims with a source and data year near the first relevant use.
- Do not demand citations for equation numbers, section numbers, sample identifiers, or clearly labelled model outputs.
- Distinguish external facts, calculated results, assumptions, methodological constants, and examples.
- Mark geographic, temporal, and methodological mismatches explicitly.
- Verify claims about repository behaviour against the current code or configuration.

## Reusable Codex review prompts

Task-specific review instructions are stored in `.codex/prompts/`:

- `explore.md`
- `config-guard.md`
- `gap-flagger.md`
- `source-checker.md`
- `test-runner.md`

Use the relevant prompt as a task checklist. These are reusable Codex prompts, not automatically spawned sub-agents.