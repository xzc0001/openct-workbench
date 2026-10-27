# Contributing

Use synthetic inputs and disposable accounts. Do not submit clinical DICOM,
derived images/geometry, exported previews, screenshots of real studies,
institution names, internal hosts or addresses, usernames, logs, credentials,
API keys, model license keys or access tokens in commits, issues or PRs.

Keep configuration local; use example.invalid placeholders in documentation.
Do not remove upstream notices or redistribute models without checking terms.
Run `python scripts/check_release.py` and the synthetic unit suite before a PR.
Review the Git index and all new file types before pushing. Clearly distinguish
engineering test results from clinical validation.
