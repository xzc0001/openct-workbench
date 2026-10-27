# Internal deployment

## Local workstation

Follow the README. The configuration helper uses the interpreter that runs it,
so invoke it from the desired virtual environment. `weights` is relative to the
`workbench` directory. The default account database is `auth/accounts.sqlite3`
outside the jobs/cache directories; `WORKBENCH_AUTH_DATABASE` can override it.
Use an absolute database path for services. Keep credentials private on Windows
with local ACLs; POSIX initialization uses restricted file modes.

The HTTP server binds to `127.0.0.1`, independently of the compatibility `host`
configuration key. For internal sharing, place an authenticated HTTPS reverse
proxy in front of it. Add the exact browser origin, for example
`https://ct.example.invalid`, to your **local** `public_origins` list. The example
domain is a placeholder and must be replaced locally. Configure proxy headers,
request limits and forwarding trust deliberately; do not trust client-supplied
identity headers from an exposed backend.

## Linux systemd / resource isolation

Optional `WORKBENCH_RESOURCE_ISOLATION=1` requires cgroup v2 and systemd delegation
of `cpu memory pids`. The implementation expects a system-level service cgroup,
creates separate web/compute groups, and fails if isolation is unavailable.
`WORKBENCH_WORKER_BUDGET_BYTES` sets the compute memory budget (default 3 GiB);
the web group has its own limit. Large native CT bone workloads may require
substantially more memory and will be rejected rather than silently downsampled.

An operator-authored service should use a dedicated account, `UMask=0077`,
`NoNewPrivileges=yes`, protected system paths and explicit writable directories.
Use `--unix-socket /YOUR_PRIVATE_RUN_DIRECTORY/workbench.sock` when sharing a socket
with a proxy. The server currently creates that socket with mode 0666: protect its
parent directory with dedicated group ownership and restrictive permissions.
Do not put it in a world-traversable directory. Configure system-level network
isolation for the compute process; Python socket guards are defense in depth.

The optional `health_watchdog.py` contains generic service/container identifiers
and a loopback gateway default. It is **not installed or scheduled automatically**.
Adapt service names, runtime paths and gateway environment variables to your
deployment before enabling it. Do not run its recovery commands against unrelated
services. No original deployment unit files are distributed.

## Optional static portal

`portal/scripts/publish.py` imports a single trusted self-contained HTML through
a JSON request on stdin, verifies its expected hash and embedded-resource
structure, stores versioned previews and rebuilds a catalog from `portal/site`.
It is a POSIX helper (uses `fcntl`); create private `incoming/`, `records/` and
the static `public/` under `portal/` before use. Read the request validation in
the script and integrate it into your own authenticated transfer workflow.

This portal is not an anonymizer and does not sanitize arbitrary active HTML.
The bundled-resource check is structural, not a JavaScript security sandbox.
Only admit trusted generator output. Authentication, owner-aware access and TLS
must protect **both catalog and every case URL**. A static URL alone grants no
permission boundary. The baseline does not publish a portal, open a network port,
create a DNS entry or include any original reverse-proxy/server configuration.

## Operations

Keep source DICOM immutable; store derived volumes, masks, meshes and previews
separately. Back up authentication and active studies before upgrades or cleanup.
Validate health, unauthorized access rejection, upload ownership, restart recovery
and model availability in your own environment. Real-data use needs separate
clinical, privacy and operational acceptance.
