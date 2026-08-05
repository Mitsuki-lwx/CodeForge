---
name: test
description: 运行测试，修复失败，补写缺失的测试
allowed_tools:
  - bash
  - read_file
  - glob
  - grep
  - write_file
  - edit_file
mode: inline
---
# Test Skill

You are a test engineer. When activated, follow this SOP:

## Step 1: Discover Test Setup

Look for existing test configurations:
- `pytest` in `pyproject.toml` or `setup.cfg`
- `conftest.py` files
- Test directories (`tests/`, `test/`, `__tests__/`)
- `Makefile` with test targets

Run `glob` with pattern `**/test_*.py` and `**/conftest.py` to discover test files.

## Step 2: Run Existing Tests

Run the test suite using the discovered configuration:
- `pytest tests/ -v` for pytest projects
- `python -m pytest tests/ -v` as fallback
- If no test runner is configured, use `pytest` as default

Capture all output. If all tests pass, report success and go to Step 4.

## Step 3: Fix Failing Tests

For each failing test:
1. Read the test file to understand what it asserts
2. Read the source code being tested
3. Identify why the test fails (code bug vs test assertion wrong)
4. Fix the code or the test accordingly
5. Re-run the test to confirm it passes
6. Repeat until all tests pass

## Step 4: Check for Missing Tests

After all existing tests pass:
1. Look at recently changed source files (via `git diff --name-only` or by scanning the project)
2. Check if those files have corresponding test files
3. If any source file lacks tests, write basic tests covering:
   - Happy path
   - Edge cases
   - Error handling

Use the project's existing test patterns (fixtures, assertions, naming conventions).

## Step 5: Final Run

Run the full test suite one final time. Report:
- Total tests run
- Passed / Failed / Skipped
- Any new tests added

## Constraints
- Do NOT modify working tests unless they test the wrong behavior
- Match the project's existing test style
- If the project has no tests at all, create a minimal test structure
