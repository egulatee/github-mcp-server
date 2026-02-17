#!/usr/bin/env python3
"""
MCP access-control filter for github-mcp-server.

Sits between the MCP client and `github-mcp-server stdio`, filtering tool
calls to enforce org/repo access restrictions and a safe default tool list
that excludes merge_pull_request.

Environment variables
---------------------
GITHUB_PERSONAL_ACCESS_TOKEN  (required) Passed through to github-mcp-server.
GITHUB_TOOLS                  Comma-separated allowlist of MCP tool names.
                               Defaults to ALL_TOOLS_DEFAULT (see below).
ALLOWED_ORGS                  Comma-separated list of GitHub org/user names
                               that agents are permitted to access.  Supports
                               fnmatch globs, e.g. "myorg,partner-*".
ALLOWED_REPOS                 Comma-separated list of "owner/repo" patterns.
                               Supports fnmatch globs, e.g. "myorg/*,other/specific-repo".

Access-control logic
--------------------
* If NEITHER ALLOWED_ORGS nor ALLOWED_REPOS is set, all org/repo access is
  permitted (pass-through mode — rely on PAT scoping instead).
* A tool call is allowed when the owner matches ANY pattern in ALLOWED_ORGS
  OR the full "owner/repo" matches ANY pattern in ALLOWED_REPOS.
* Tools that carry no owner/repo arguments (e.g. get_me) are always allowed.
* When ALLOWED_ORGS or ALLOWED_REPOS is set, tool calls that supply `repo`
  but omit `owner` are rejected (ambiguous — owner is required for matching).
* search_* tools use a free-text query string; org/repo filtering is NOT
  applied to their arguments — rely on PAT scoping for those.
"""

import fnmatch
import json
import os
import subprocess
import sys
import threading

# ---------------------------------------------------------------------------
# Default tool allowlist — merge_pull_request intentionally absent.
# Operators may override by setting GITHUB_TOOLS in the pod environment.
# ---------------------------------------------------------------------------
ALL_TOOLS_DEFAULT = (
    "get_file_contents,list_branches,list_commits,get_commit,"
    "create_branch,push_files,create_or_update_file,delete_file,"
    "create_pull_request,list_pull_requests,pull_request_read,"
    "pull_request_review_write,add_comment_to_pending_review,"
    "update_pull_request,update_pull_request_branch,"
    "issue_read,issue_write,add_issue_comment,list_issues,"
    "list_issue_types,sub_issue_write,"
    "search_code,search_repositories,search_pull_requests,search_issues,"
    "search_users,get_status,get_me,get_label,"
    "fork_repository,create_repository,"
    "get_latest_release,get_release_by_tag,list_releases,list_tags,get_tag,"
    "request_copilot_review"
)

if "GITHUB_TOOLS" not in os.environ:
    os.environ["GITHUB_TOOLS"] = ALL_TOOLS_DEFAULT

# ---------------------------------------------------------------------------
# Hardcoded blocked tools — cannot be overridden by any environment variable.
# merge_pull_request is permanently blocked regardless of GITHUB_TOOLS.
# ---------------------------------------------------------------------------
BLOCKED_TOOLS: frozenset[str] = frozenset({"merge_pull_request"})

# ---------------------------------------------------------------------------
# Tool allowlist — built from GITHUB_TOOLS (already defaulted above).
# A tools/call request is rejected if the tool name is not in this set.
# ---------------------------------------------------------------------------
ALLOWED_TOOLS: frozenset[str] = frozenset(
    t.strip() for t in os.environ["GITHUB_TOOLS"].split(",") if t.strip()
)

# ---------------------------------------------------------------------------
# Access-control configuration
# ---------------------------------------------------------------------------
ALLOWED_ORGS: list[str] = [
    o.strip() for o in os.environ.get("ALLOWED_ORGS", "").split(",") if o.strip()
]
ALLOWED_REPOS: list[str] = [
    r.strip() for r in os.environ.get("ALLOWED_REPOS", "").split(",") if r.strip()
]

# ---------------------------------------------------------------------------
# Synthetic tool: get_access_policy
# Injected into tools/list responses and handled locally (never forwarded).
# ---------------------------------------------------------------------------
_GET_ACCESS_POLICY_TOOL: dict = {
    "name": "get_access_policy",
    "description": (
        "Returns the active MCP access-control policy: tool allowlist, "
        "permanently blocked tools, and org/repo restrictions."
    ),
    "inputSchema": {"type": "object", "properties": {}, "required": []},
}

# ---------------------------------------------------------------------------
# State for tools/list response injection (thread-safe)
# ---------------------------------------------------------------------------
_tools_list_ids: set = set()
_tools_list_lock = threading.Lock()


def is_allowed(owner: str | None, repo: str | None) -> bool:
    """Return True if the owner/repo combination is permitted."""
    if not ALLOWED_ORGS and not ALLOWED_REPOS:
        return True  # no restrictions configured

    # repo without owner is ambiguous — reject when restrictions are active
    if repo and not owner:
        return False

    if owner:
        for pattern in ALLOWED_ORGS:
            if fnmatch.fnmatch(owner, pattern):
                return True

    if owner and repo:
        full = f"{owner}/{repo}"
        for pattern in ALLOWED_REPOS:
            if fnmatch.fnmatch(full, pattern):
                return True

    return False


def make_error(msg_id: object, text: str) -> bytes:
    resp = {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {
            "code": -32600,
            "message": text,
        },
    }
    return (json.dumps(resp) + "\n").encode()


def make_result(msg_id: object, content: object) -> bytes:
    resp = {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": content,
    }
    return (json.dumps(resp) + "\n").encode()


def handle_get_access_policy(msg_id: object) -> bytes:
    """Synthesise a tools/call result for the get_access_policy tool."""
    policy = {
        "allowed_tools": sorted(ALLOWED_TOOLS),
        "blocked_tools": sorted(BLOCKED_TOOLS),
        "allowed_orgs": ALLOWED_ORGS,
        "allowed_repos": ALLOWED_REPOS,
        "mode": "restricted" if (ALLOWED_ORGS or ALLOWED_REPOS) else "passthrough",
    }
    return make_result(
        msg_id,
        {"content": [{"type": "text", "text": json.dumps(policy, indent=2)}]},
    )


def check_message(msg: dict) -> bytes | None:
    """
    Returns an encoded response if the message should be intercepted,
    or None if it should be forwarded to github-mcp-server.
    """
    method = msg.get("method")

    # Track tools/list requests so we can inject our synthetic tool later.
    if method == "tools/list":
        msg_id = msg.get("id")
        if msg_id is not None:
            with _tools_list_lock:
                _tools_list_ids.add(msg_id)
        return None  # forward to upstream

    if method != "tools/call":
        return None

    params = msg.get("params", {})
    tool_name: str | None = params.get("name")

    # Synthetic tool: handle locally before any allowlist check.
    if tool_name == "get_access_policy":
        return handle_get_access_policy(msg.get("id"))

    # Hard block: permanently forbidden tools regardless of GITHUB_TOOLS.
    if tool_name in BLOCKED_TOOLS:
        return make_error(
            msg.get("id"),
            f"Tool '{tool_name}' is permanently disabled",
        )

    # Allowlist enforcement: reject tools not in GITHUB_TOOLS.
    if tool_name not in ALLOWED_TOOLS:
        return make_error(
            msg.get("id"),
            f"Tool '{tool_name}' is not permitted",
        )

    args = params.get("arguments", {})
    owner: str | None = args.get("owner")
    repo: str | None = args.get("repo")

    # Only check when the tool actually carries owner/repo parameters
    if owner is not None or repo is not None:
        if not is_allowed(owner, repo):
            target = f"{owner}/{repo}" if repo else str(owner)
            return make_error(
                msg.get("id"),
                f"Access denied: '{target}' is not in ALLOWED_ORGS or ALLOWED_REPOS",
            )

    return None


def inject_synthetic_tools(line: bytes) -> bytes:
    """
    If *line* is a tools/list response for a tracked request ID, inject
    the get_access_policy synthetic tool into the tools list.
    Returns the (possibly modified) line.
    """
    try:
        resp = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return line

    msg_id = resp.get("id")
    with _tools_list_lock:
        is_tracked = msg_id in _tools_list_ids
        if is_tracked:
            _tools_list_ids.discard(msg_id)

    if not is_tracked or "result" not in resp:
        return line

    result = resp["result"]
    if not isinstance(result, dict):
        return line

    tools = result.get("tools")
    if not isinstance(tools, list):
        return line

    tools.append(_GET_ACCESS_POLICY_TOOL)
    result["tools"] = tools
    return (json.dumps(resp) + "\n").encode()


def main() -> None:
    proc = subprocess.Popen(
        ["github-mcp-server", "stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
    )

    def forward_output() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = inject_synthetic_tools(line)
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
        except BrokenPipeError:
            pass

    threading.Thread(target=forward_output, daemon=True).start()

    assert proc.stdin is not None
    try:
        for raw_line in sys.stdin.buffer:
            if not raw_line.strip():
                continue
            try:
                msg = json.loads(raw_line)
                intercept_resp = check_message(msg)
                if intercept_resp:
                    sys.stdout.buffer.write(intercept_resp)
                    sys.stdout.buffer.flush()
                else:
                    proc.stdin.write(raw_line)
                    proc.stdin.flush()
            except json.JSONDecodeError:
                # Forward non-JSON lines as-is (should not occur in MCP)
                proc.stdin.write(raw_line)
                proc.stdin.flush()
    except BrokenPipeError:
        pass
    finally:
        proc.stdin.close()

    proc.wait()
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
