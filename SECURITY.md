# Security Policy

GitWise takes the security of repository data, source code, credentials, and the systems that process them seriously. This policy explains which releases receive security fixes and how to report a suspected vulnerability responsibly.

## Supported Versions

GitWise is currently in early development and has not reached a 1.0 release. Security fixes are provided for the latest supported release line.

| Version | Supported |
| ------- | --------- |
| 0.1.x (latest) | :white_check_mark: |
| Earlier 0.x releases | :x: |
| Pre-release builds and development snapshots | :x: |

Support status may change as the project approaches a stable release. Users should upgrade to the latest supported release before reporting an issue where practical.

## Reporting a Vulnerability

**Please do not report suspected security vulnerabilities through public GitHub issues, pull requests, or discussions.** Public disclosure before a fix is available can put users and downstream projects at risk.

Use GitHub's private vulnerability reporting flow for this repository:

1. Open the repository's **Security** tab.
2. Select **Report a vulnerability** (private vulnerability reporting / security advisory).
3. Include the details requested below.

If the private reporting option is unavailable, do not publish the details publicly. Contact the repository maintainers through the GitHub profile for [virtuewise](https://github.com/virtuewise) and request a private channel for a security report.

### What to include

Please provide as much of the following as you can safely share:

- A clear description of the vulnerability and its potential impact.
- The affected GitWise version, commit, component, and deployment configuration, if known.
- Reproduction steps or a minimal proof of concept.
- Any prerequisites, permissions, or user interaction required.
- Relevant logs, request samples, or screenshots, with secrets and personal data removed.
- A suggested mitigation, if you have one.

Please avoid accessing, modifying, or deleting data that does not belong to you; disrupting services; or testing against systems without authorization. Do not include live credentials, access tokens, private repository contents, or other sensitive data in a report.

### Response and disclosure expectations

- **Initial acknowledgement:** We aim to acknowledge a report within **5 business days**.
- **Updates:** We aim to provide a meaningful status update at least every **10 business days** while the report is being investigated.
- **Assessment:** We will investigate the report, assess severity and affected versions, and may ask for additional information to reproduce the issue.
- **If accepted:** We will work on a fix or mitigation, coordinate a release where appropriate, and credit the reporter if they wish to be credited.
- **If declined:** We will explain why the report does not meet the project's vulnerability criteria, where we can do so, and may suggest an appropriate alternative reporting route.

These are response targets, not guaranteed resolution deadlines. The time needed to validate and fix an issue depends on its complexity, severity, and the availability of a safe mitigation.

We ask reporters to allow reasonable time for investigation and coordinated remediation before public disclosure. We will coordinate disclosure timing with the reporter where possible and will not ask reporters to conceal an unresolved issue indefinitely.

## Scope

This policy covers security vulnerabilities in GitWise's own code, official packages, and project-operated services. Examples include:

- Authentication, authorization, and access-control weaknesses.
- Exposure or mishandling of GitHub tokens, credentials, or private repository data.
- Server-side request forgery, injection, path traversal, and unsafe handling of repository content.
- Vulnerabilities in repository ingestion, parsing, storage, or agent execution boundaries.
- Cross-tenant data access, sensitive information disclosure, and exploitable denial of service.

Issues in third-party services or dependencies should generally be reported to their respective maintainers as well. If a third-party issue creates a demonstrable vulnerability in GitWise, please report the GitWise impact privately as described above.

Thank you for helping keep GitWise and its users safe.
