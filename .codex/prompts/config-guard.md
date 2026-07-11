# Review configuration and architecture

Review the specified change without editing it. Be direct and evidence-based.

## Checks

1. **Explicit configuration** — modelling scripts require `--config`; required inputs do not receive silent fallbacks.
2. **Region-agnostic logic** — model behaviour does not branch on hardcoded countries or regions. Country names are acceptable in tests, labels, documentation, and diagnostics when they do not control logic.
3. **Scenario identity** — Integrated Assessment Model data are matched using `(model, scenario)` pairs.
4. **Double counting** — changes to demand, electrification, income elasticity, price elasticity, or total final energy do not represent an effect already included elsewhere without documented separation.
5. **Gap flagging** — missing, interpolated, proxied, transferred, or assumed values remain visible and traceable.

## Output

For each applicable check, report `PASS` or `FAIL`. For failures, include:

- file and line
- the violated rule
- why it matters
- the expected pattern, preferably with a conforming repository example

Do not rewrite the code and do not add praise. If all applicable checks pass, say so briefly.
