# Web dependency advisory closure

The baseline audit's “six advisories” were **six affected packages spanning eight
unique GHSA identifiers**. All six were marked `dev: true` in the original lock.
The gateway UI is plain JavaScript. The worker ships a compiled playground and
runs Python, with no Node runtime or node_modules in its container. That bounds
production exposure; it does not make build/developer vulnerabilities harmless.

The targeted command `npm update @vitest/mocker baseline-browser-mapping
browserslist nanoid postcss vitest --package-lock-only --ignore-scripts --no-audit`
selected compatible releases inside the existing dependency declarations.
No framework migration or dependency manifest change was required. The updated
lock is consumed by `npm ci`; the web assets are rebuilt from that lock.

| Package | Baseline → fixed lock | Relationship / class | Baseline severity | Advisory / CVE and patched version in the selected major | Disposition |
|---|---|---|---|---|---|
| `@vitest/mocker` | 4.1.9 → 4.1.11 | Transitive dev/build/test | moderate | [GHSA-82fw-gwwq-j7x9](https://github.com/advisories/GHSA-82fw-gwwq-j7x9) / CVE-2026-84373; fixed 4.1.11 | Compatible update; closed |
| `baseline-browser-mapping` | 2.10.37 → 2.11.23 | Transitive dev/build/test | moderate | [GHSA-w5vr-8v7q-w6rv](https://github.com/advisories/GHSA-w5vr-8v7q-w6rv) / CVE-2026-45819; fixed 2.11.0 | Compatible update; closed |
| `browserslist` | 4.28.2 → 4.28.9 | Transitive dev/build/test | high | [GHSA-c83g-rgw3-j3cx](https://github.com/advisories/GHSA-c83g-rgw3-j3cx) / CVE-2026-73089; fixed 4.28.7<br>[GHSA-73wf-gq98-2v4g](https://github.com/advisories/GHSA-73wf-gq98-2v4g) / CVE-2026-73088; fixed 4.28.7 | Compatible update; closed |
| `nanoid` | 3.3.12 → 3.3.19 | Transitive dev/build/test | high | [GHSA-28wg-ghj8-5hjv](https://github.com/advisories/GHSA-28wg-ghj8-5hjv) / CVE-2026-67214; fixed 3.3.16<br>[GHSA-2v37-7h3g-55p8](https://github.com/advisories/GHSA-2v37-7h3g-55p8) / CVE-2026-67213; fixed 3.3.18 | Compatible update; closed |
| `postcss` | 8.5.15 → 8.5.28 | Transitive dev/build/test | high | [GHSA-fxqj-rqcc-2cmp](https://github.com/advisories/GHSA-fxqj-rqcc-2cmp) / CVE-2026-69153; fixed 8.5.23<br>[GHSA-r28c-9q8g-f849](https://github.com/advisories/GHSA-r28c-9q8g-f849) / CVE-2026-73646; fixed 8.5.18 | Compatible update; closed |
| `vitest` | 4.1.9 → 4.1.11 | Direct dev/build/test | moderate | [GHSA-82fw-gwwq-j7x9](https://github.com/advisories/GHSA-82fw-gwwq-j7x9) / CVE-2026-84373; fixed 4.1.11 | Compatible update; closed |

## Exposure assessment

- **@vitest/mocker:** Test/dev-server redirect mocks. Not loaded by the Python worker or static production UI; tests use one-shot jsdom, not a publicly exposed mock WebSocket server.

- **vitest:** Direct test runner; shares the mocker advisory. No Vitest server is started in production.

- **baseline-browser-mapping:** Build-time browser-target resolution. Invalid mapping inputs could terminate the build process; inference requests do not supply browser targets.

- **browserslist:** Build-time browser queries/custom statistics. Unbounded distinct queries or malicious stats concern build input, not a deployed gateway endpoint.

- **nanoid:** Transitive build tooling under PostCSS. Invalid sizes concern generator callers in Node tooling; no Node runtime is present in the worker image.

- **postcss:** Build-time CSS/source-map handling. Malicious CSS inputs can affect a build host; the deployed worker serves prebuilt files and never runs PostCSS on client input.

The baseline high severities for browserslist, nanoid and PostCSS are retained
above; individual PostCSS advisories include both moderate and high severities.
The final audit must include development dependencies, not just `--omit=dev`.
No advisory is waived or accepted unresolved. Historical commits retain their
historical dependency locks; the supported release/build path is the final HEAD.
