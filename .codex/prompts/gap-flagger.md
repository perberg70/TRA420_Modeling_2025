# Review factual claims and data gaps

Review the specified Markdown, report, or thesis text without rewriting it.

## Checks

1. Externally derived numeric claims have an attributable source and data year near their first relevant use.
2. Known uncertainty, missing-country coverage, proxy transfer, interpolation, and disagreement between sources are stated explicitly.
3. Assumptions are not presented as findings.
4. Figures with different years, methods, geographic scopes, or definitions are not compared without a mismatch note.
5. Claims about model behaviour match the current code and configuration.

Do not flag equation numbers, headings, sample identifiers, or clearly labelled model outputs merely because they contain numbers. Distinguish external facts, calculated results, assumptions, methodological constants, and examples.

## Output

For each flagged passage, provide:

- a short quotation
- the violated check
- what is missing or mismatched
- file and line where possible

Comment only on flagged passages. End with: `X passages flagged out of Y factual or numeric claims reviewed.`
