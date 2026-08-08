# Security Policy

QGIS Agent MCP controls a live desktop GIS and can read or mutate project state. Security reports are treated as a priority.

## Supported versions

Security fixes are provided for the latest published release. Users should upgrade to the newest release before reporting a problem that may already be fixed.

| Version | Supported |
| --- | --- |
| 0.4.x latest release | Yes |
| Older releases | No |

## Report a vulnerability privately

Use [GitHub private vulnerability reporting](https://github.com/Aaa2122/QGIS-MCP/security/advisories/new). If that channel is unavailable, email **auguste.sagaert@gmail.com** with the subject `QGIS Agent MCP security report`.

Please include:

- affected version and operating system;
- QGIS and MCP client versions;
- impact and realistic attack scenario;
- minimal reproduction steps or a proof of concept;
- suggested mitigation, if known.

Do not include real credentials, authentication databases, private datasets or confidential QGIS projects. Use synthetic data and redact paths, tokens and connection details.

We aim to acknowledge a report within 72 hours, provide an initial assessment within seven days and coordinate a fix and disclosure timeline according to severity. Please allow a reasonable remediation period before public disclosure.

## Security boundaries

The bridge is intended for authenticated loopback use. Arbitrary Python execution is not exposed. Tools that access networks, files, plugins or project mutations must keep their explicit confirmation, validation, revision and idempotency controls.
