# Security policy

## Reporting a vulnerability

Please do not report suspected vulnerabilities through public GitHub issues. Until a dedicated security contact is published, report them privately to **massi.gentili@gmail.com** with the subject line `Taktstock security report`.

Include a concise description, affected component or version, reproduction steps, and the likely impact. We will acknowledge reports within seven days and coordinate a fix and disclosure timeline with the reporter.

## Deployment responsibility

Taktstock can orchestrate tools that access source code and external services. Operators must:

- keep secrets outside the repository and rotate exposed credentials immediately;
- restrict network access and user permissions;
- review model-provider data handling before transmitting any sensitive material;
- use explicit approval gates for consequential changes;
- update dependencies and apply security fixes promptly.

The project is experimental software. It does not provide a compliance certification or a substitute for an organisation's security and data-protection assessment.
