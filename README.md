# Taktstock

> Open-source, self-hosted governance infrastructure for responsible AI-assisted software work.

Taktstock helps small civic-tech teams and social or environmental organisations use AI coding agents without surrendering control of their code, data, review process, or operational budget. It is built for teams that need practical automation, but cannot accept a black box making unreviewed changes to their systems.

## Why Taktstock

AI agents can accelerate software work, yet their deployment often creates four linked problems: source code and prompts may leave the organisation; actions can be difficult to reconstruct; autonomous changes can bypass meaningful human review; and repeated large-model calls consume more resources than the task warrants.

Taktstock is self-hosted and modular, keeps the human decision-maker in control of consequential changes, and makes execution constraints visible.

## Current foundation

This repository contains the working technical foundation of the **Taktstock Multi-Agent Orchestrator**. It already includes:

- isolated Git workspaces for concurrent tasks;
- structured multi-agent planning and review flows;
- visual Git-diff review and explicit approval gates before critical changes;
- token and call-budget controls, compact context handling, and run telemetry;
- local-first runtime components, Docker deployment, and restricted host-side bridges;
- tests for security-sensitive paths, repositories, budgets, queues, and orchestration.

## Prototype focus — 2026

The Taktstock prototype will turn this foundation into reusable public-interest infrastructure through four new, testable components:

1. **Project policy manifest** — versioned, human-readable rules for approved tools, permitted data classes, action boundaries, and call budgets.
2. **Approval and evidence record** — an exportable record of task, policy version, agent/tool, result, and human decision. Sensitive prompts and source code are not retained by default.
3. **Sufficiency controls** — configurable limits and routing rules that avoid unnecessary model calls, oversized context, and repeated work; transparent operational metrics report what was constrained and why.
4. **Pilot toolkit** — installation, threat-model, and evaluation materials for small civic-tech and mission-driven teams running the system themselves.

The goal is not to automate software delivery without people. The goal is to make AI-assisted work inspectable, governable, and proportionate to the task.

## Design principles

- **Human control**: consequential code changes require an explicit review decision.
- **Data minimisation**: collect and retain only what is needed to run and evidence a task.
- **Portability**: avoid hard-wiring a team to a single model provider; deployments choose their approved backends.
- **Digital sufficiency**: use bounded context, budgets, and task-appropriate execution rather than treating greater model use as inherently better.
- **Open practice**: publish code, documentation, implementation learnings, and reusable governance templates.

## Status and limits

Taktstock is an early prototype, not a security certification or a guarantee of legal compliance. Existing integrations may rely on third-party model providers; users remain responsible for their provider choices, data-protection obligations, and deployment security. The prototype will document its assumptions and limitations rather than obscure them.

## Quick start (development)

The core runs on Python's standard library; optional packages support tests and visual design capture.

```bash
cd server
python3 -m pip install -r requirements.txt
python3 health_server.py
```

For deployment and operational guidance, see [server/README.md](server/README.md), [OPERATIONS.md](OPERATIONS.md), and [SECURITY.md](SECURITY.md). Never commit credentials: start from [.env.example](.env.example).

## Contributing

Contributions are welcome, especially from teams working on public-interest technology, responsible AI operations, privacy engineering, and low-resource deployment. Please read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) before opening an issue or pull request.

## Licence

Copyright 2026 Massimo Gentili and contributors. Licensed under the [MIT License](LICENSE).
