# Source release validation

This record describes the sanitized foundation, with synthetic tests only.
It does not certify clinical performance or a fresh installation on every OS.

## 2026-11-05 update

- Release allowlist: 152 UTF-8 files, including 13 Wiki Markdown files (11 content
  pages, sidebar and footer); all 86 Python source files parsed successfully.
- Workbench: 130 tests, 128 passed and 2 platform-limited skips, in approximately
  102 seconds. Cluster: 40 tests, 39 passed and 1 platform-limited skip, in
  approximately 21 seconds. The final complete runs passed.
- The original online-preview fixture lacked study metadata required by the new
  access checks. The synthetic fixture was corrected without relaxing access
  control. A transient Windows saturated-handler health probe failed in one
  earlier run; its isolated rerun and the final complete suite passed.
- Additional Node.js contracts for video lifecycle and volume sampling/memory
  budget passed. Browser rendering, final printer output and native node DLL
  execution were not validated as part of this publication.
- Clean-source bootstrap, random administrator initialization, health/login and
  anonymous denial of job, guest-management and node-management endpoints passed.
- Source policy checks, Gitleaks 8.30.1 and a private 74-term local-identifier
  comparison found no sensitive data in the 152 release files. Wiki text also
  passed its independent source scan and internal page-link checks.
- Verification used an existing Python 3.13 dependency environment plus isolated
  cryptography test dependencies; no clinical studies or deployed services were
  changed. This does not prove a fresh installation on every supported platform.
- Before pushing, staged contents and reachable Git history are checked again;
  remote commit/tree identity is verified after upload. Actual network targets,
  certificates, keys, account state, engines and model packages stay excluded.

GitHub Wiki publication uses a separate repository and requires an initial page
to have been created on GitHub. The complete reviewable source is included under
`docs/wiki/` regardless of that initialization state.

## Checks completed before the first upload

- Explicit allowlist: 91 UTF-8 source, template, notice and documentation files.
  No original repository/history or runtime/data directories were copied.
- All 48 Python source files parsed successfully.
- Clean allowlisted copy bootstrap: generated local configuration and random
  administrator, started the HTTP service, confirmed health/login responses and
  anonymous job-list rejection (401). This reused an installed interpreter and
  dependencies; it was not a fresh dependency installation.
- Full unittest discovery: **82 tests, 81 passed, 1 skipped** in approximately
  133 seconds. The skipped case checks stale Unix socket handling on a host
  where that socket binding is unavailable.
- Viewer JavaScript contracts ran with Node.js; the separate UI/lighting/momentum
  invocation also passed. These are contract/unit checks, not a complete browser
  or GPU visual acceptance test.
- Gitleaks 8.30.1 source scan with redacted findings: zero findings.
- Release policy scan: zero findings for token formats, private keys, credential
  URLs, non-loopback IPs, unreviewed domains, local user paths, institutional
  naming patterns, unexpected file types and embedded study payloads.
- A separate private inventory comparison against local patient/environment
  identifiers found zero matches. Its term list and local audit output remain
  outside the repository.

The same release gate passed on the staged index and reachable Git objects, and
Gitleaks found zero findings in the fresh initial commit. These checks are
repeated after any final documentation edits before pushing. The first commit has no parent; no old history is
imported. Audit logs, authentication state and machine-specific configuration
are excluded from the release.

## Scope and limitations

Synthetic tests cover ingestion attacks, authorization/ownership/cleanup,
physical geometry, original-value preview transport, progressive workflows,
resource recovery policy and viewer controls. VTK emitted deprecation warnings
for `vtkTransformPolyDataFilter`; these did not fail tests.

The source release was exercised with an existing isolated dependency runtime,
not by downloading and installing every dependency into a clean host. No model
weights are distributed and no full clinical-image inference or production
Linux service deployment was performed as part of publication. OS-level cgroup
delegation, reverse-proxy/TLS behavior, real GPU rendering and clinical/anatomical
acceptance must be validated independently at the destination.

No scanner proves the absence of all possible sensitive information. The release
uses source selection, manual review, independent secret detection, local
identifier matching and Git-object review together.
