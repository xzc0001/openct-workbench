# Source release privacy boundaries

This repository is assembled from an explicit source allowlist in a fresh
directory. No original Git history, image data, rendered clinical HTML, runtime
environment, model weights, account database, credentials, logs, deployment
configuration, screenshots or operational handoff notes are included.

`config.example.json` contains loopback and portable relative paths. Local config
is created after cloning. External services, institution names and internal
endpoints are not needed to run the baseline. No cloud API key is required.

Before any initial upload:

1. Review every selected file and all comments, strings, templates and notices.
2. Check names/paths, URLs, non-loopback addresses, institutions and case-specific
   identifiers as well as API keys, passwords, private keys and embedded payloads.
3. Run the release checker on source, then on the staged Git index and reachable
   Git objects. Review commit author/email/message before adding any remote.
4. Create only a fresh initial commit of the reviewed tree. Never copy an old
   repository or upload a sensitive version intending to remove it later.

`scripts/check_release.py` is an additional fail-closed text/type policy gate,
not a guarantee of complete anonymization or a substitute for manual review.
The release allowlist and file manifest help detect accidental new data files.
Keep local deny terms and audit output outside the repository.

## Runtime data remain sensitive

The application handles imaging data. Even without name fields, source CT,
volumes, segmentations, meshes, screenshots, derived HTML, notes and logs can be
personal medical data. Exported HTML embeds imaging payloads intentionally and
must remain within authorized storage and sharing boundaries. This project does
not scrub DICOM, remove burned-in identifiers or guarantee data anonymization.

Git ignores are a convenience, not access control. Never force-add ignored data.
If a real secret is ever disclosed, revoke or rotate it and follow incident
response, rather than relying only on a later deletion commit.
