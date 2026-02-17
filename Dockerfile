# syntax=docker/dockerfile:1

# ── Stage 1: pull the upstream binary ────────────────────────────────────────
# The upstream image is distroless (gcr.io/distroless/base-debian12).
# The Go binary is CGO_ENABLED=0 (statically linked), so it runs fine on musl.
ARG UPSTREAM_VERSION=v0.30.3
FROM ghcr.io/github/github-mcp-server:${UPSTREAM_VERSION} AS upstream

# ── Stage 2: minimal Alpine runtime ──────────────────────────────────────────
FROM alpine:3.21

# Copy the statically-linked binary from the distroless stage
COPY --from=upstream /server/github-mcp-server /usr/local/bin/github-mcp-server

# Install socat (Unix-socket bridge) and python3 (access-control filter)
RUN apk add --no-cache socat python3 \
 && adduser -D -u 65532 -g "" nonroot

# Copy the MCP access-control filter
COPY filter.py /usr/local/bin/mcp-filter.py
RUN chmod +x /usr/local/bin/mcp-filter.py

USER nonroot

# Default: run as a Kubernetes sidecar, exposing the server on a Unix socket.
# Override CMD (or exec the binary directly) for stdio / HTTP use.
CMD ["sh", "-c", \
  "socat UNIX-LISTEN:/var/run/mcp/github.sock,fork,reuseaddr \
   EXEC:'python3 /usr/local/bin/mcp-filter.py'"]
