"""
Unit tests for filter.py access-control logic.

Run with:  pytest test_filter.py -v
"""

import json
import pytest
import filter as f


@pytest.fixture(autouse=True)
def reset_tools_list_ids():
    """Ensure _tools_list_ids is clean before and after every test."""
    f._tools_list_ids.clear()
    yield
    f._tools_list_ids.clear()


# ---------------------------------------------------------------------------
# is_allowed()
# ---------------------------------------------------------------------------


class TestIsAllowed:
    def test_no_restrictions_permits_all(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_ORGS", [])
        monkeypatch.setattr(f, "ALLOWED_REPOS", [])
        assert f.is_allowed(None, None) is True
        assert f.is_allowed("anyorg", "anyrepo") is True
        assert f.is_allowed(None, "anyrepo") is True

    def test_repo_without_owner_rejected_when_orgs_restricted(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_ORGS", ["myorg"])
        monkeypatch.setattr(f, "ALLOWED_REPOS", [])
        assert f.is_allowed(None, "somerepo") is False

    def test_repo_without_owner_rejected_when_repos_restricted(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_ORGS", [])
        monkeypatch.setattr(f, "ALLOWED_REPOS", ["myorg/somerepo"])
        assert f.is_allowed(None, "somerepo") is False

    def test_owner_in_allowed_orgs_permitted(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_ORGS", ["myorg"])
        monkeypatch.setattr(f, "ALLOWED_REPOS", [])
        assert f.is_allowed("myorg", "anyrepo") is True

    def test_owner_not_in_allowed_orgs_denied(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_ORGS", ["myorg"])
        monkeypatch.setattr(f, "ALLOWED_REPOS", [])
        assert f.is_allowed("badorg", "anyrepo") is False

    def test_org_glob_pattern_matches(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_ORGS", ["partner-*"])
        monkeypatch.setattr(f, "ALLOWED_REPOS", [])
        assert f.is_allowed("partner-foo", "repo") is True
        assert f.is_allowed("partner-bar", "repo") is True
        assert f.is_allowed("other-foo", "repo") is False

    def test_repo_in_allowed_repos_permitted(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_ORGS", [])
        monkeypatch.setattr(f, "ALLOWED_REPOS", ["myorg/specific"])
        assert f.is_allowed("myorg", "specific") is True
        assert f.is_allowed("myorg", "other") is False

    def test_repo_glob_pattern_matches(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_ORGS", [])
        monkeypatch.setattr(f, "ALLOWED_REPOS", ["myorg/*"])
        assert f.is_allowed("myorg", "anything") is True
        assert f.is_allowed("other", "anything") is False

    def test_owner_only_no_repo_with_orgs_allowed(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_ORGS", ["myorg"])
        monkeypatch.setattr(f, "ALLOWED_REPOS", [])
        assert f.is_allowed("myorg", None) is True

    def test_owner_only_no_repo_with_repos_only_denied(self, monkeypatch):
        # Only ALLOWED_REPOS configured — no repo argument means no match
        monkeypatch.setattr(f, "ALLOWED_ORGS", [])
        monkeypatch.setattr(f, "ALLOWED_REPOS", ["myorg/repo"])
        assert f.is_allowed("myorg", None) is False

    def test_allowed_via_repos_not_orgs(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_ORGS", ["otherorg"])
        monkeypatch.setattr(f, "ALLOWED_REPOS", ["myorg/myrepo"])
        assert f.is_allowed("myorg", "myrepo") is True  # matches ALLOWED_REPOS
        assert f.is_allowed("myorg", "other") is False


# ---------------------------------------------------------------------------
# check_message()
# ---------------------------------------------------------------------------


class TestCheckMessage:
    def test_non_tools_call_forwarded(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        assert f.check_message(msg) is None

    def test_notifications_forwarded(self):
        msg = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        assert f.check_message(msg) is None

    def test_blocked_tool_rejected(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "merge_pull_request", "arguments": {}},
        }
        resp = f.check_message(msg)
        assert resp is not None
        parsed = json.loads(resp)
        assert "error" in parsed
        assert "permanently disabled" in parsed["error"]["message"]

    def test_tool_not_in_allowlist_rejected(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_TOOLS", frozenset({"get_me"}))
        msg = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "delete_file", "arguments": {}},
        }
        resp = f.check_message(msg)
        assert resp is not None
        parsed = json.loads(resp)
        assert "error" in parsed
        assert "not permitted" in parsed["error"]["message"]

    def test_allowed_tool_no_owner_repo_forwarded(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_TOOLS", frozenset({"get_me"}))
        monkeypatch.setattr(f, "ALLOWED_ORGS", ["myorg"])
        monkeypatch.setattr(f, "ALLOWED_REPOS", [])
        msg = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_me", "arguments": {}},
        }
        assert f.check_message(msg) is None

    def test_repo_without_owner_rejected_when_restricted(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_TOOLS", frozenset({"get_file_contents"}))
        monkeypatch.setattr(f, "ALLOWED_ORGS", ["myorg"])
        monkeypatch.setattr(f, "ALLOWED_REPOS", [])
        msg = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "get_file_contents",
                "arguments": {"repo": "somerepo"},
            },
        }
        resp = f.check_message(msg)
        assert resp is not None
        parsed = json.loads(resp)
        assert "error" in parsed
        assert "Access denied" in parsed["error"]["message"]

    def test_owner_in_allowlist_forwarded(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_TOOLS", frozenset({"get_file_contents"}))
        monkeypatch.setattr(f, "ALLOWED_ORGS", ["myorg"])
        monkeypatch.setattr(f, "ALLOWED_REPOS", [])
        msg = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "get_file_contents",
                "arguments": {"owner": "myorg", "repo": "myrepo"},
            },
        }
        assert f.check_message(msg) is None

    def test_owner_not_in_allowlist_rejected(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_TOOLS", frozenset({"get_file_contents"}))
        monkeypatch.setattr(f, "ALLOWED_ORGS", ["myorg"])
        monkeypatch.setattr(f, "ALLOWED_REPOS", [])
        msg = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "get_file_contents",
                "arguments": {"owner": "badorg", "repo": "repo"},
            },
        }
        resp = f.check_message(msg)
        assert resp is not None
        parsed = json.loads(resp)
        assert "error" in parsed

    def test_get_access_policy_returns_result_restricted(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_ORGS", ["myorg"])
        monkeypatch.setattr(f, "ALLOWED_REPOS", ["myorg/repo"])
        monkeypatch.setattr(f, "ALLOWED_TOOLS", frozenset({"get_me"}))
        msg = {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {"name": "get_access_policy", "arguments": {}},
        }
        resp = f.check_message(msg)
        assert resp is not None
        parsed = json.loads(resp)
        assert "result" in parsed
        assert "error" not in parsed
        assert parsed["id"] == 42
        policy = json.loads(parsed["result"]["content"][0]["text"])
        assert policy["mode"] == "restricted"
        assert "myorg" in policy["allowed_orgs"]
        assert "myorg/repo" in policy["allowed_repos"]
        assert "get_me" in policy["allowed_tools"]

    def test_get_access_policy_returns_passthrough_mode(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_ORGS", [])
        monkeypatch.setattr(f, "ALLOWED_REPOS", [])
        monkeypatch.setattr(f, "ALLOWED_TOOLS", frozenset({"get_me"}))
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_access_policy", "arguments": {}},
        }
        resp = f.check_message(msg)
        assert resp is not None
        parsed = json.loads(resp)
        policy = json.loads(parsed["result"]["content"][0]["text"])
        assert policy["mode"] == "passthrough"

    def test_get_access_policy_bypasses_tool_allowlist(self, monkeypatch):
        # get_access_policy should work even if it's not in ALLOWED_TOOLS
        monkeypatch.setattr(f, "ALLOWED_TOOLS", frozenset())  # nothing allowed
        monkeypatch.setattr(f, "ALLOWED_ORGS", [])
        monkeypatch.setattr(f, "ALLOWED_REPOS", [])
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_access_policy", "arguments": {}},
        }
        resp = f.check_message(msg)
        assert resp is not None
        parsed = json.loads(resp)
        assert "result" in parsed

    def test_tools_list_request_tracked(self):
        msg = {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}
        result = f.check_message(msg)
        assert result is None  # forwarded to upstream
        assert 7 in f._tools_list_ids

    def test_tools_list_notification_not_tracked(self):
        # Notifications have no id — should not add None to tracking set
        msg = {"jsonrpc": "2.0", "method": "tools/list", "params": {}}
        f.check_message(msg)
        assert None not in f._tools_list_ids

    def test_error_response_preserves_id(self, monkeypatch):
        monkeypatch.setattr(f, "ALLOWED_TOOLS", frozenset({"get_me"}))
        msg = {
            "jsonrpc": "2.0",
            "id": "req-abc",
            "method": "tools/call",
            "params": {"name": "merge_pull_request", "arguments": {}},
        }
        resp = f.check_message(msg)
        parsed = json.loads(resp)
        assert parsed["id"] == "req-abc"


# ---------------------------------------------------------------------------
# inject_synthetic_tools()
# ---------------------------------------------------------------------------


class TestInjectSyntheticTools:
    def test_non_json_line_unchanged(self):
        line = b"not json\n"
        assert f.inject_synthetic_tools(line) == line

    def test_untracked_id_unchanged(self):
        resp = {"jsonrpc": "2.0", "id": 99, "result": {"tools": [{"name": "get_me"}]}}
        line = (json.dumps(resp) + "\n").encode()
        assert f.inject_synthetic_tools(line) == line

    def test_tracked_tools_list_gets_injected(self):
        f._tools_list_ids.add(5)
        resp = {"jsonrpc": "2.0", "id": 5, "result": {"tools": [{"name": "get_me"}]}}
        line = (json.dumps(resp) + "\n").encode()
        result = f.inject_synthetic_tools(line)
        parsed = json.loads(result)
        tool_names = [t["name"] for t in parsed["result"]["tools"]]
        assert "get_access_policy" in tool_names
        assert "get_me" in tool_names

    def test_tracked_id_consumed_only_once(self):
        f._tools_list_ids.add(5)
        resp = {"jsonrpc": "2.0", "id": 5, "result": {"tools": []}}
        line = (json.dumps(resp) + "\n").encode()
        f.inject_synthetic_tools(line)  # first call — injects and removes ID
        # second call — ID is gone, should not inject again
        resp2 = {"jsonrpc": "2.0", "id": 5, "result": {"tools": []}}
        line2 = (json.dumps(resp2) + "\n").encode()
        result2 = f.inject_synthetic_tools(line2)
        parsed2 = json.loads(result2)
        assert parsed2 == resp2  # unchanged

    def test_error_response_not_modified(self):
        f._tools_list_ids.add(5)
        resp = {"jsonrpc": "2.0", "id": 5, "error": {"code": -32600, "message": "err"}}
        line = (json.dumps(resp) + "\n").encode()
        result = f.inject_synthetic_tools(line)
        parsed = json.loads(result)
        assert "result" not in parsed
        assert "error" in parsed

    def test_result_without_tools_key_unchanged(self):
        f._tools_list_ids.add(5)
        resp = {"jsonrpc": "2.0", "id": 5, "result": {"other": "data"}}
        line = (json.dumps(resp) + "\n").encode()
        result = f.inject_synthetic_tools(line)
        parsed = json.loads(result)
        assert "tools" not in parsed["result"]

    def test_injected_tool_has_correct_name(self):
        f._tools_list_ids.add(10)
        resp = {"jsonrpc": "2.0", "id": 10, "result": {"tools": []}}
        line = (json.dumps(resp) + "\n").encode()
        result = f.inject_synthetic_tools(line)
        parsed = json.loads(result)
        injected = parsed["result"]["tools"][0]
        assert injected["name"] == "get_access_policy"
        assert "inputSchema" in injected
        assert "description" in injected
