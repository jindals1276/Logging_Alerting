# Contributing

## Branch Protection

The `main` branch is protected. All changes must go through a pull request with passing unit tests before merging.

## Workflow

1. Create a feature branch from `main`
2. Make your changes
3. Run tests locally before pushing:
   ```bash
   python -m unittest discover -s tests -v
   ```
4. Push your branch and open a pull request
5. Wait for the **Run Tests** CI check to pass
6. Request a code review (see below)
7. Merge once the review is complete and tests pass

## Code Review

All pull requests should be reviewed before merging. You can request a review from any team member using the **Reviewers** section in the PR sidebar.

### Code Review with GitHub Copilot CLI

Before opening a PR, you can use the GitHub Copilot CLI to get an AI-powered review of your changes locally:

```bash
# Review all uncommitted changes
gh copilot review

# Review changes between your branch and main
gh copilot review --diff "main...HEAD"
```

> **Prerequisite:** Install the [GitHub Copilot CLI extension](https://docs.github.com/en/copilot/github-copilot-in-the-cli) with `gh extension install github/gh-copilot`

## Running Tests

```bash
# Run all tests
python -m unittest discover -s tests -v

# Run only black-box integration tests
python -m unittest tests.test_blackbox -v

# Run a specific test class
python -m unittest tests.test_blackbox.TestAlertTriggering -v

# Run a single test
python -m unittest tests.test_blackbox.TestFiltering.test_mixed_batch_only_qualifying_counted -v
```

No external dependencies are needed — all tests use Python stdlib.
