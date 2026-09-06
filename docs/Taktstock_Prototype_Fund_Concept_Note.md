---
title: "Taktstock"
subtitle: "Concept note for Prototype Fund Switzerland 2026"
author: "Massimo Gentili, Irma, Valentina Guariglia"
date: "September 2026"
---

# Open-source governance for responsible AI-assisted software work

**Primary challenge:** Digital Sovereignty & Trusted Data for AI  
**Repository:** https://github.com/massigent/taktstock

## The problem

Small civic-tech and mission-driven teams increasingly use AI coding agents without the governance capacity of large organisations. Code and prompts may leave their control, permissions can remain hidden in tool defaults, and agent actions can be difficult to reconstruct or review. Repeated large-model calls and oversized context also create avoidable resource use.

## The prototype

Taktstock is an open-source, self-hosted governance layer for AI-assisted software work. It will build on an existing orchestration foundation and deliver four reusable components:

1. **Versioned project policies** for approved tools, data classes, action boundaries and budgets.
2. **Explicit human approval** linked to visual code diffs before push or merge.
3. **Minimised execution evidence** recording policy, tool/agent, outcome and human decision without retaining raw prompts or code by default.
4. **Digital-sufficiency controls** that bound calls and context, making task-appropriate model use visible.

## Responsible and sustainable by design

Taktstock makes governance tangible in the daily workflow: policies make delegation inspectable; review gates preserve accountability; self-hosting options and minimised records support privacy; and isolated workspaces limit execution scope. It reports operational proxies such as calls, token budgets, retries and context size rather than unsubstantiated emissions figures.

The project does not claim that every responsible decision is automatically sustainable. It will document trade-offs, including the tension between detailed audit records and privacy, or between local control and the resources required to run it.

## Four-month delivery path

| Month | Outcome |
|---|---|
| 1 | User interviews, workflow mapping, threat model and public specification |
| 2 | Policy manifests, pre-execution checks and approval flows |
| 3 | Evidence records, budget/context controls and usability evaluation |
| 4 | Open-source pilot release, templates, deployment guide and learning report |

## Team

**Massimo Gentili** leads product, technical architecture and backend implementation. **Irma** leads user research, UX and validation. **Valentina Guariglia** leads interaction design, information architecture and pilot experience. Together, the team combines working agent-orchestration software with user-centred, social and environmental design expertise.

## Intended public value

Taktstock is not another coding agent. It is reusable infrastructure that helps smaller organisations make informed choices about AI delegation, data boundaries, human oversight and resource use. The code, policy templates, threat-model checklist, evaluation method and learning report will be openly available.
