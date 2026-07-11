# Run and summarize tests

Run the requested test file, path, or `-k` selection. When no scope is given, run the full suite.

## Rules

- Always use `pytest --basetemp=./.pytest_tmp <args>`.
- Do not edit code or tests.
- Do not paste raw test output.
- Separate environment and dependency failures from logic or assertion failures.

## Output

Begin with: `N passed, N failed, N skipped (Xs)`.

When failures exist, report for each:

- test identifier (`file::test`)
- concise assertion or error
- failing file and line, when available
- classification: logic failure or environment failure

When everything passes, stop after the summary line.
