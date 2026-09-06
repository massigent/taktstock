# Taktstock — Prototype Fund Switzerland 2026 application draft

**Primary challenge:** Digital Sovereignty & Trusted Data for AI

All text below is in English and written to fit the stated limits. Confirm personal details, rates, and any challenge-owner information before submitting.

## Project name (80)

Taktstock

## Project description (600)

Taktstock is an open-source, self-hosted governance layer for AI-assisted software work. It helps small civic-tech teams and mission-driven organisations retain control over their code and data, require human approval for consequential changes, and make AI-agent activity understandable and proportionate to the task. During the prototype, we will turn an existing orchestration foundation into reusable policy, evidence, and sufficiency controls.

## Challenge area

**Digital Sovereignty & Trusted Data for AI.** Taktstock gives smaller organisations practical agency over where agentic AI runs, what it may access, which tools and models are permitted, and how its actions are reviewed. It does not promise full independence from every provider; it creates the technical and organisational conditions to make deliberate, documented choices rather than accepting opaque defaults.

## Problem (600)

Small public-interest teams increasingly use AI coding agents but lack the governance capacity of large organisations. Code and prompts can leave their control; permissions and model choices remain hidden in tool defaults; agent actions are hard to reconstruct; and automation can weaken meaningful review. Repeated large-model calls and oversized context also create avoidable resource use. This matters now because AI-assisted development is becoming routine before transparent, affordable governance practices are available to the smaller organisations that need them.

## Proposed solution, prototype, and MVP (600)

Taktstock is a self-hosted, open-source governance layer for AI-assisted development. Its MVP adds four components to a working orchestration foundation: (1) a versioned policy manifest for approved tools, data classes, action boundaries and budgets; (2) human approval gates linked to visual code diffs; (3) an exportable record of policy, tool/agent, outcome and human decision, without retaining sensitive prompts or code by default; and (4) sufficiency controls that bound calls and context. We will test it with representative public-interest workflows.

## Responsible and Sustainable AI (1200)

Taktstock treats responsible AI as an operational practice. A readable, versioned policy manifest and execution record show which tool or agent was used, under which limits, and what human decision followed. Consequential code changes remain reviewable through a visual diff and cannot be silently accepted. Governance is embodied in configurable boundaries for tools, repositories, data classes and call budgets.

Privacy and security are addressed through self-hosted deployment options, data minimisation, restricted runtime bridges and a default that records operational metadata rather than raw prompts or code. Taktstock will document when selected model providers remain a data boundary; it will not claim sovereignty where a deployment chooses a third-party service.

Environmental sustainability is addressed through digital sufficiency: bounded task budgets, compact context and task-appropriate routing reduce unnecessary calls and repeated work. We will report operational proxies—calls, token budgets, retries and context size—rather than unsubstantiated emissions figures. Public value comes from making these practices reusable by smaller teams otherwise dependent on opaque defaults.

## Learning questions and success (600)

Taktstock should answer whether small teams can govern agentic software work without creating an unusable compliance burden. Which controls make a reviewer genuinely informed? What is sufficient evidence while preserving confidentiality? Can bounded context and call budgets reduce resource use without undermining quality or inclusion? Success means a functioning MVP, policies and threat model, three representative workflow evaluations, and public implementation learnings. Afterwards, success means pilot adoption and reusable governance patterns beyond this tool.

## Intended users and validation (600)

Initial users are Swiss civic-tech/open-source teams and small social or environmental organisations that maintain software but cannot surrender control of repositories, decisions or costs to a closed platform. We will use interviews, co-design around existing workflows and task-based tests of policy setup, review and evidence export. Validation asks whether participants understand what the system permits, can complete a review decision, and find the controls useful rather than obstructive. We will recruit through Swiss design, civic-tech and impact networks.

## Governance, legal, ethical and societal questions (1200)

The core question is who remains responsible for decisions around agent-produced code. Taktstock separates assistance from authority: agents may propose and execute bounded work, while a person approves consequential changes. A versioned policy makes delegation explicit: allowed tools, data sensitivity, repository scope, budget and required review can be inspected rather than hidden in a prompt or vendor dashboard.

The prototype raises data-protection questions whenever code, prompts or logs contain confidential or personal information. We will minimise data, avoid retaining raw prompts and code in evidence records by default, document provider-boundary risks, and include a checklist for access, retention and incident response. It is not legal-compliance automation.

Agentic systems can concentrate power in organisations able to buy opaque infrastructure. Open policies, documentation and a self-hosted reference give smaller teams a practical alternative. The prototype will make its limits visible: no interface replaces informed judgement, and governance quality also depends on organisational practice.

## Sufficiency and sustainability (1200)

For Taktstock, sufficiency means asking whether an AI call, a larger context window, a retry, or a stronger model is necessary for the task at hand before it is executed. The project will operationalise this through project-level budgets, compact context handling, bounded task scope, and routing rules that favour an adequate option over an automatically maximal one. The user can see the applied constraints and override them deliberately when a task justifies it.

This is broader than efficiency. The intended long-term outcome is to help organisations develop a culture of deliberate AI use: preserving human review where it adds value, avoiding automation that merely shifts risk, and selecting infrastructure that matches actual needs rather than novelty or scale assumptions. Taktstock will make operational signals visible—calls, retries, context size and budget use—but will not convert them into unverified carbon claims. We will instead publish a transparent method, its limits, and the questions that remain for more reliable environmental measurement.

## Responsible–sustainable trade-offs (1200)

Trade-offs are expected. More detailed audit records improve transparency and accountability but can increase storage, privacy exposure and operational overhead. Stronger human review may slow delivery; removing review can be cheaper in the short term but transfers safety and accountability costs to users and organisations. Running a local or regionally controlled model can improve data control but may require hardware, energy or expertise that a small team does not have. A smaller model or reduced context can lower resource use, but may increase errors, retries or unfair exclusion of complex language and accessibility needs.

The prototype will not hide these tensions behind a single score. It will allow teams to state their priorities in policy, record exceptions, and evaluate outcomes in representative tasks. We will document cases where a higher-resource choice was justified, as well as cases where a restriction protected data or prevented waste. The key learning is what decision process is credible and usable for smaller organisations, not an assertion that every responsible choice is automatically sustainable.

## Focus parameters and response (600)

**Primary focus:** transparency; accountability and governance; privacy and security; sustainability and sufficiency; public-interest technology.

Policies define constraints; visual diffs and approval gates make decisions inspectable; minimised records enable accountability; and budgets make resource use discussable. Fairness and robustness matter in model selection and testing but are not the central claim. Challenges include understandable policies, non-surveillant logs, and constraints that preserve quality and accessibility.

## Similar approaches and differentiation (600)

AI coding assistants, agent frameworks and hosted platforms already offer planning, execution or collaboration. Some provide logs, review screens or sandboxing. Taktstock differs by treating governance and sufficiency as portable project artefacts, not platform settings: a team can inspect a versioned policy, link it to approval, and export a minimal record. It combines an existing foundation—isolated workspaces, visual diff review, budget controls and restricted bridges—into an open, self-hosted reference for small public-interest teams with templates and documented trade-offs.

## Technical implementation and work division (600)

The MVP is a Python, Docker-deployable service using Git worktrees, SQLite for optional local state, a review web interface, and pluggable approved backends. New modules implement a versioned policy manifest, pre-execution enforcement, minimised records, budget/context controls and exportable reports. No model is trained.

Massimo leads architecture, backend, security and open-source delivery. Irma leads research, workflow design and usability. Valentina leads information architecture, design system and pilot experience. The team jointly conducts tests and synthesis.

## Team suitability (600)

Massimo Gentili created the orchestration foundation and brings direct experience coordinating AI agents, constraining execution, reviewing changes and managing budgets. Irma designed the winning “SustAInable Shopper” solution at #HERhack2023 Switzerland for a Migros sustainability challenge using anonymised customer data. Valentina Guariglia is a Zürich-based Product & Interaction Designer with an MA from SUPSI and experience in complex systems, social design and soil-monitoring/carbon-farming workflows. Together we combine implementation, responsible UX and environmental/social design.

## Missing competencies/resources and workaround (600)

The team does not yet have a formal pilot organisation or dedicated legal/privacy counsel. We will recruit three prospective users through Swiss civic-tech, design and impact networks; use structured co-design and evaluation; and seek targeted expert input through the Fund network. For legal and data-protection questions, we will publish a boundary statement and deployment checklist rather than compliance claims. We prioritise a narrow, usable MVP over unsupported enterprise-readiness claims.

## Current stage (600)

The project has a functional technical foundation, initially created for the founder’s own software-development workflow. It includes multi-agent planning, isolated Git worktrees, visual diff review, approval paths, token/call budgets, telemetry and security-oriented components. It has not yet been shaped as a public-interest governance tool, tested with external organisations, or released through a responsible-and-sustainable AI journey. The four-month period formalises the policy and evidence layer, validates it with intended users, releases it openly and documents findings.

## Four milestones (600)

1. **Month 1:** interviews, workflow mapping, threat model, sufficiency hypotheses and public specification.
2. **Month 2:** policy manifests, pre-execution checks and approval flows; representative-task tests.
3. **Month 3:** minimised records, budget/context controls and export; usability and governance evaluation.
4. **Month 4:** publish code, documentation, templates, deployment guide and learning report.

## Continuation beyond Prototype Fund (600)

Taktstock continues as an open-source project with a documented self-hosted reference deployment, modular adapters and public governance templates. Adoption starts with pilot participants and the team’s civic-tech, open-source and impact networks. Its policy, evidence and approval patterns can transfer to agentic workflows beyond software development. Continued work prioritises community contributions, locally controlled or approved backends, independently funded pilots and partnerships with organisations needing trustworthy AI-assisted work. Core public artefacts remain openly available.

## Risks and mitigation (600)

Risks include backend integration and policy bypass; mitigation is a narrow MVP, pre-execution enforcement, isolated workspaces, approval gates and tests. Privacy/security risks arise from code, prompts, tokens and logs; mitigation includes self-hosting options, restricted access, secret handling, minimisation and no raw prompt/code retention by default. Automation bias and false confidence are addressed by foregrounding limits and preserving human decisions. Rebound effects are addressed through operational proxies, documented exceptions and no unsupported carbon claims.

## Insights for governance and policy (600)

The project will generate evidence about the minimum governance information a small organisation needs for agentic AI: which policy choices must be explicit, what a meaningful approval record looks like, and where transparency conflicts with confidentiality or usability. It also tests how sufficiency can be operationalised before model invocation through scope, budget and context decisions. Code, templates, a threat-model checklist, evaluation protocol and learning report will be public. Others can reuse both components and documented trade-offs rather than copy an opaque “best practice”.

## Preferred Challenge Owner / partner

We would value a Challenge Owner or partner with expertise in digital sovereignty, trustworthy data governance, responsible-AI assurance and adoption by smaller Swiss organisations. We are especially interested in partners able to challenge the prototype’s assumptions about data boundaries, public value and realistic deployment conditions.

## Why / additional ideal partners (600)

The prototype needs critical domain input more than generic technology validation. A partner rooted in Swiss digital sovereignty or public-interest infrastructure could test whether the policy and evidence patterns are understandable and transferable. We also welcome a sustainability partner to scrutinise our sufficiency method, and a civic-tech, NGO or small mission-driven organisation as pilot user. These partners keep the project accountable to organisational constraints and prevent us designing only for technically confident developers.

## Additional information (600)

The existing codebase was created to solve a real personal workflow need, which gives the team a working foundation and direct knowledge of the risks of agentic development. The funding is not sought to market a finished product. It is sought to transform this foundation into a narrow, validated and openly reusable public-interest prototype: one that makes delegation, data boundaries, human approval and resource constraints explicit. We welcome scrutiny of the project’s limits and will share negative findings as well as successes.

## Video pitch outline (maximum 3 minutes)

**0:00–0:25 — problem.** “AI agents are entering everyday software work before smaller organisations have practical ways to govern them. Code and prompts can leave their control; automation can become unreviewed; and more AI use is often treated as inherently better.”

**0:25–1:15 — solution.** “Taktstock is open-source, self-hosted governance infrastructure for AI-assisted software work. It lets a small team define what an agent may access and do, review meaningful changes visually, and keep a minimal record of what happened and who approved it.”

**1:15–1:55 — sustainability.** “We also ask ‘how much is enough?’ We will use budgets, compact context and task-appropriate routing to avoid unnecessary calls and make resource choices visible. We will report what we can measure honestly, not invent carbon numbers.”

**1:55–2:35 — team and prototype.** “Massimo brings the technical foundation; Irma and Valentina bring user-centred, social and environmental design. In four months we will build the policy, approval, evidence and sufficiency controls, and test them with prospective users.”

**2:35–3:00 — ask.** “Prototype Fund Switzerland can help us turn a personal technical tool into shared public-interest infrastructure, test its trade-offs with experts and users, and publish the code and lessons for others.”
