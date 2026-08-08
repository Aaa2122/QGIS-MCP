# Governance

QGIS Agent MCP is an open-source project maintained by [@Aaa2122](https://github.com/Aaa2122).

## Decision making

Routine changes are decided through Pull Request review. Decisions prioritize, in order:

1. user and project-data safety;
2. QGIS and MCP protocol correctness;
3. reproducible end-to-end results;
4. context efficiency and execution performance;
5. compatibility and maintainability.

The maintainer seeks community input for material API, security, compatibility or governance changes. When consensus is not possible, the maintainer records the decision and its trade-offs in the relevant Pull Request or Discussion.

## Roles

- **Contributors** propose changes, report problems and participate in review.
- **Reviewers** provide technical feedback but do not gain merge authority automatically.
- **Maintainers** triage reports, protect releases, merge changes and enforce project policies.

Additional maintainers may be invited after sustained, constructive contributions and demonstrated care for security, QGIS compatibility and the community.

## Releases

Releases use semantic versioning while the project remains pre-1.0: minor releases may add substantial capabilities, and patch releases focus on compatible fixes. Every release should have a tagged commit, release notes, an installable ZIP and passing Python, QGIS LTR and QGIS 4 checks.

## Policy changes

Changes to governance, security or conduct policies should be made through a dedicated Pull Request with a clear explanation and community review period when practical.
