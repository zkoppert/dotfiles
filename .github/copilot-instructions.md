## GitHub Actions Best Practices
When creating or modifying GitHub Actions workflows:
1. Always use the **latest release** of each action
2. Pin actions to their **full commit SHA** (not tags) with a comment showing the human-readable version: `uses: actions/checkout@<sha> # v6.0.2`
3. Validate workflow syntax before committing
