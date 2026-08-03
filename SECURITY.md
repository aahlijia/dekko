# Security Policy

## Supported Versions

dekko is pre-1.0 and released as a single rolling line — only the
latest version published to [PyPI](https://pypi.org/project/dekko/)
receives fixes.

| Version        | Supported |
| -------------- | --------- |
| latest release | ✅        |
| older releases | ❌        |

## Reporting a Vulnerability

dekko parses source code locally with tree-sitter and does not execute
it or make network calls at parse time, so its attack surface is small.
If you find a security issue anyway (e.g. a crafted repository that
triggers unsafe behavior in the CLI or MCP server), please report it
privately rather than opening a public issue:

- Email **aahlijia@gmail.com** with a description and, if possible, a
  minimal reproducer.
- You can expect an initial response within a few days.

Please do not disclose the issue publicly until it has been addressed.
