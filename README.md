# github-mcp-server

Thin wrapper image over [`ghcr.io/github/github-mcp-server`](https://github.com/github/github-mcp-server) that adds `socat` and a Python access-control filter for Unix socket bridging in Kubernetes sidecar deployments.

## Why This Image Exists

The upstream `ghcr.io/github/github-mcp-server` is a distroless Go binary — no shell, no utilities. To expose the MCP server on a Unix domain socket inside a Kubernetes pod (so other containers can connect to it), we need `socat`. This image adds:

- **`socat`** — bridges the MCP server's stdin/stdout to a Unix socket
- **`filter.py`** — MCP access-control layer that enforces a tool allowlist and org/repo restrictions

Rather than patching the upstream image, this repo copies the statically-linked binary into a minimal Alpine runtime and adds the required utilities.

## Image

```
ghcr.io/egulatee/github-mcp-server:latest
ghcr.io/egulatee/github-mcp-server:v0.30.3   # tracks upstream version
```

## Usage as a Kubernetes Sidecar

### Minimal sidecar spec

```yaml
containers:
  - name: github-mcp
    image: ghcr.io/egulatee/github-mcp-server:latest
    env:
      - name: GITHUB_PERSONAL_ACCESS_TOKEN
        valueFrom:
          secretKeyRef:
            name: openclaw-secrets
            key: github-token
    volumeMounts:
      - name: mcp-sockets
        mountPath: /var/run/mcp
    resources:
      requests:
        cpu: "100m"
        memory: "128Mi"
      limits:
        cpu: "500m"
        memory: "512Mi"

volumes:
  - name: mcp-sockets
    emptyDir: {}
```

The container's default `CMD` runs:

```bash
socat UNIX-LISTEN:/var/run/mcp/github.sock,fork,reuseaddr \
      EXEC:'python3 /usr/local/bin/mcp-filter.py'
```

This exposes the MCP server on `/var/run/mcp/github.sock`. Mount the same `emptyDir` into your main container and connect over the Unix socket.

### Full spec with access-control

```yaml
containers:
  - name: github-mcp
    image: ghcr.io/egulatee/github-mcp-server:latest
    env:
      - name: GITHUB_PERSONAL_ACCESS_TOKEN
        valueFrom:
          secretKeyRef:
            name: openclaw-secrets
            key: github-token
      # Restrict which tools agents may call (merge_pull_request excluded by default)
      - name: GITHUB_TOOLS
        value: >-
          get_file_contents,list_branches,list_commits,get_commit,
          create_branch,push_files,create_or_update_file,
          create_pull_request,list_pull_requests,pull_request_read,
          pull_request_review_write,issue_read,issue_write,
          add_issue_comment,list_issues,search_code,search_repositories,
          search_pull_requests,get_status,get_me,get_label
      # Restrict which GitHub orgs/users agents may access
      - name: ALLOWED_ORGS
        value: "myorg,partner-*"
      # Restrict to specific repos (supports fnmatch globs)
      - name: ALLOWED_REPOS
        value: "myorg/app,myorg/infra-*"
    volumeMounts:
      - name: mcp-sockets
        mountPath: /var/run/mcp
    resources:
      requests:
        cpu: "100m"
        memory: "128Mi"
      limits:
        cpu: "500m"
        memory: "512Mi"
```

> **Note**: `merge_pull_request` is intentionally absent from the default tool list — agents cannot merge; humans merge.

### Connecting from the main container

```python
import socket, json

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect("/var/run/mcp/github.sock")

# Send MCP initialize request
request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {...}}
sock.sendall((json.dumps(request) + "\n").encode())
response = sock.makefile().readline()
```

## Access-Control Filter (`filter.py`)

The `filter.py` script sits between the MCP client and `github-mcp-server stdio`, providing:

| Environment Variable        | Description |
|-----------------------------|-------------|
| `GITHUB_PERSONAL_ACCESS_TOKEN` | **Required.** Passed through to the MCP server. |
| `GITHUB_TOOLS`              | Comma-separated allowlist of MCP tool names. Defaults to all tools except `merge_pull_request`. |
| `ALLOWED_ORGS`              | Comma-separated list of GitHub org/user names. Supports `fnmatch` globs (e.g. `myorg,partner-*`). |
| `ALLOWED_REPOS`             | Comma-separated list of `owner/repo` patterns. Supports globs (e.g. `myorg/*`). |

### Access-control logic

1. **Pass-through mode** — if neither `ALLOWED_ORGS` nor `ALLOWED_REPOS` is set, all org/repo access is permitted (rely on PAT scoping).
2. **Restricted mode** — when either variable is set:
   - A call is **allowed** when the `owner` matches any `ALLOWED_ORGS` pattern **or** the full `owner/repo` matches any `ALLOWED_REPOS` pattern.
   - A call that provides `repo` but **omits `owner`** is **rejected** — the owner is required for allowlist matching and an omitted owner is treated as a misconfiguration.
   - Tools with no owner/repo arguments (e.g. `get_me`) are always allowed.
3. `search_*` tools use free-text queries — org/repo filtering is not applied to their arguments; rely on PAT scoping for those.
4. `merge_pull_request` is **permanently blocked** regardless of `GITHUB_TOOLS`.

Pattern syntax follows Python [`fnmatch`](https://docs.python.org/3/library/fnmatch.html): `*` matches any sequence of characters, `?` matches a single character.

### Recommended configuration patterns

| Goal | Configuration |
|------|--------------|
| Restrict to a single org | `ALLOWED_ORGS=myorg` |
| Restrict to specific repos | `ALLOWED_REPOS=myorg/app,myorg/infra` |
| Restrict to org + wildcard repos | `ALLOWED_ORGS=myorg` / `ALLOWED_REPOS=myorg/*` |
| Allow partner orgs by prefix | `ALLOWED_ORGS=myorg,partner-*` |
| Lock down to one repo | `ALLOWED_REPOS=myorg/specific-repo` |

### Synthetic tool: `get_access_policy`

`filter.py` exposes a built-in `get_access_policy` tool that is **not forwarded to the upstream server** — it is handled locally by the filter itself. Calling it returns a JSON document describing the active policy:

```json
{
  "mode": "restricted",
  "allowed_orgs": ["myorg"],
  "allowed_repos": [],
  "allowed_tools": ["create_branch", "get_file_contents", "..."],
  "blocked_tools": ["merge_pull_request"]
}
```

`mode` is `"restricted"` when `ALLOWED_ORGS` or `ALLOWED_REPOS` is set, `"passthrough"` otherwise. This tool always works, even when the upstream server is unreachable, and bypasses the tool allowlist check so operators can inspect the policy at any time.

## Building Locally

```bash
# Standard build
docker build -t github-mcp-server:local .

# Multi-arch build (requires buildx)
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t ghcr.io/egulatee/github-mcp-server:local \
  --load \
  .

# Build with a specific upstream version
docker build \
  --build-arg UPSTREAM_VERSION=v0.30.3 \
  -t github-mcp-server:v0.30.3 \
  .
```

## Running Locally (stdio mode)

```bash
docker run --rm \
  -e GITHUB_PERSONAL_ACCESS_TOKEN=ghp_... \
  ghcr.io/egulatee/github-mcp-server:latest \
  github-mcp-server stdio
```

Send a JSON-RPC initialize request on stdin to verify the server responds.

## CI / Automated Builds

The `.github/workflows/build.yml` workflow:

- Triggers on push to `main` and on new `v*` tags
- Builds multi-arch images (`linux/amd64`, `linux/arm64`)
- Pushes to `ghcr.io/egulatee/github-mcp-server` with tags:
  - `latest` (on every push to `main`)
  - `v<version>` and `<version>` (on semver tags)
  - `sha-<short-sha>` (for traceability)
- Uses `GITHUB_TOKEN` — no external secrets required

## Tag Versioning

Image tags track the upstream `github-mcp-server` version:

| Image tag    | Upstream version |
|--------------|-----------------|
| `latest`     | latest build from `main` |
| `v0.30.3`    | `ghcr.io/github/github-mcp-server:v0.30.3` |

To upgrade to a new upstream release, bump `ARG UPSTREAM_VERSION` in the `Dockerfile` and push a new tag.

## Related

- Upstream: [github/github-mcp-server](https://github.com/github/github-mcp-server)
