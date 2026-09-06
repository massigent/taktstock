# Contributing to Taktstock

Thank you for helping make AI-assisted software work more inspectable and accountable.

## Before contributing

- Do not include credentials, production data, private source code, personal data, or customer prompts in issues, commits, screenshots, or test fixtures.
- Keep behaviour changes small and explain their impact on human control, privacy, security, and resource use.
- For security vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Development workflow

1. Create a branch from the current default branch.
2. Make a focused change with tests where practical.
3. Run the relevant test suite from the repository root:

   ```bash
   python3 -m unittest discover -s tests
   ```

4. Open a pull request describing the problem, the solution, tests run, and any trade-off introduced.

## Design review questions

Every contribution affecting agent execution should answer:

- What can the agent read, change, or transmit after this change?
- Where is explicit human control required?
- What is recorded, who can read it, and how long is it retained?
- Does this reduce or increase unnecessary model calls, context, or compute?
