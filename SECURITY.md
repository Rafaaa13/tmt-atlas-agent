# Security Policy

## Data and secrets

Never commit API keys, access tokens, private company materials, personal information, or full restricted reports. The local server reads `TMT_LLM_API_KEY` from the process environment and does not persist it.

User-added sources and settings are ignored by Git through `.gitignore`. Before publishing a repository, verify that no generated local files or exported private material are included.

## External providers

LLM and data connectors are optional. Any connector should use least-privilege credentials, explicit user consent, visible source scope, rate limits, and a local fallback. Do not bypass paywalls, robots restrictions, access controls, or provider terms.

## Reporting an issue

Please do not publish sensitive details in a public issue. Contact the repository owner privately with the affected version, reproduction steps, impact, and whether any credentials or user data may be involved.

TMT Atlas is a research assistance tool and is not an investment advisory, trading, or execution system.
