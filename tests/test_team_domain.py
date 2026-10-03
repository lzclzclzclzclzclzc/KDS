from copy import deepcopy

import pytest

from app.domain.teams import (
    TeamValidationError, narrow_capabilities, normalize_budget, normalize_limits, project_context,
    require_direct_child, require_owned_task, require_room_member,
    validate_delivery, validate_entry_nodes, validate_role,
    validate_team_definition, wait_satisfied,
)


def definition():
    return {"name": "团队", "nodes": [
        {"id": key, "name": key, "role_id": "verifier", "role_version": 1}
        for key in "abcd"], "edges": [
        {"id": "ab", "source": "a", "target": "b", "type": "task"},
        {"id": "bc", "source": "b", "target": "c", "type": "task"},
        {"id": "r1", "source": "a", "target": "d", "type": "room"},
        {"id": "r2", "source": "d", "target": "c", "type": "room"},
    ]}


def test_forest_and_room_components_are_separate_and_source_is_immutable():
    source = definition()
    before = deepcopy(source)
    validated = validate_team_definition(source)
    assert source == before
    assert validated["roots"] == ["a", "d"]
    assert validated["rooms"] == [{"id": "room-1", "node_ids": ["a", "c", "d"]}]
    assert len([n for n in validated["nodes"] if n["role_id"] == "verifier"]) == 4
    # A reverse communication edge does not participate in directed cycles.
    source["edges"].append({"id": "r3", "source": "c", "target": "a", "type": "room"})
    assert validate_team_definition(source)["roots"] == ["a", "d"]


@pytest.mark.parametrize("edge,message", [
    ({"source": "c", "target": "a", "type": "task"}, "有向环"),
    ({"source": "d", "target": "b", "type": "task"}, "父节点"),
    ({"source": "missing", "target": "a", "type": "room"}, "不存在"),
    ({"source": "a", "target": "a", "type": "task"}, "自身"),
    ({"source": "b", "target": "a", "type": "room"}, "重复"),
])
def test_topology_rejects_invalid_edges_with_entity_locations(edge, message):
    value = definition()
    value["edges"].append({"id": "bad", **edge})
    with pytest.raises(TeamValidationError) as error:
        validate_team_definition(value)
    assert any(message in item["message"] for item in error.value.errors)
    if "有向环" not in message:
        assert any(item.get("edge_ids") == ["bad"] for item in error.value.errors)


def test_single_node_and_root_entry_are_supported():
    value = definition()
    value["nodes"] = value["nodes"][:1]
    value["edges"] = []
    normalized = validate_team_definition(value)
    assert normalized["rooms"] == []
    assert validate_entry_nodes(normalized, ["a"]) == ["a"]
    with pytest.raises(TeamValidationError):
        validate_entry_nodes(validate_team_definition(definition()), ["b"])


@pytest.mark.parametrize("bad", [True, False, -2, float("nan"), float("inf"), "100", 1.5])
def test_limits_reject_non_integer_or_non_finite_token_budgets(bad):
    with pytest.raises(TeamValidationError):
        normalize_limits({"single_max_tokens": bad})


def test_limits_allow_finite_duration_only_and_require_finite_session_total():
    limits = normalize_limits({"total_max_tokens": None, "total_duration_seconds": 1.25})
    assert limits["total_duration_seconds"] == 1.25
    with pytest.raises(TeamValidationError):
        normalize_limits({"total_max_tokens": None, "total_duration_seconds": None})
    with pytest.raises(TeamValidationError):
        normalize_limits([])


def test_role_validation_preserves_explicit_no_tools():
    role = validate_role({"name": "审查", "system_prompt": "校验", "tools": [],
                          "default_budget": {"single_max_tokens": 10}})
    assert role["tools"] == []
    assert role["default_budget"] == {"single_max_tokens": 10}
    with pytest.raises(TeamValidationError):
        validate_role({"name": "审查", "tools": "all"})


def test_child_capabilities_only_tighten_and_empty_tools_does_not_inherit():
    parent = {"tools": ["read", "write"], "model_config_id": "approved",
              "file_roots": ["/workspace"], "budget": {"single_max_tokens": 20}}
    assert narrow_capabilities(parent)["tools"] == ["read", "write"]
    child = narrow_capabilities(parent, {"tools": [], "budget": {"single_max_tokens": 10}})
    assert child["tools"] == []
    assert child["budget"]["single_max_tokens"] == 10
    assert parent["budget"]["single_max_tokens"] == 20
    for proposed in ({"tools": ["bash"]}, {"file_roots": ["/"]},
                     {"model_config_id": "unapproved"}, {"budget": {"single_max_tokens": 21}}):
        with pytest.raises(TeamValidationError):
            narrow_capabilities(parent, proposed)


def test_scoped_ownership_and_delivery_prevent_cross_branch_work():
    require_direct_child("a", "b", definition()["edges"])
    with pytest.raises(TeamValidationError):
        require_direct_child("a", "c", definition()["edges"])
    child = {"id": "child", "parent_task_id": "parent", "status": "running"}
    require_owned_task("parent", child)
    with pytest.raises(TeamValidationError):
        require_owned_task("unrelated", child)
    with pytest.raises(TeamValidationError):
        validate_delivery({"action": "complete_task", "speech": "已完成"}, [child])
    with pytest.raises(TeamValidationError):
        validate_delivery({"action": "wait_children", "wait": {"task_ids": ["other"]}}, [child], "parent")
    delivered = validate_delivery({"action": "wait_children", "wait": {
        "task_ids": ["child"], "mode": "any_success"}}, [child], "parent")
    assert delivered["wait"] == {"task_ids": ["child"], "mode": "any_success"}


def test_any_success_wakes_for_terminal_failure_cancel_mix_and_already_completed_children():
    wait = {"task_ids": ["one", "two"], "mode": "any_success"}
    tasks = [{"id": "one", "status": "failed"}, {"id": "two", "status": "cancelled"}]
    assert wait_satisfied(wait, tasks)
    tasks[1]["status"] = "running"
    assert not wait_satisfied(wait, tasks)
    tasks[0]["status"] = "succeeded"
    assert wait_satisfied(wait, tasks)
    assert not wait_satisfied({**wait, "mode": "all"}, tasks)


def test_room_membership_interval_and_private_context_do_not_leak():
    room = {"id": "room", "members": [{"instance_id": "new", "joined_seq": 5},
                                           {"instance_id": "old", "joined_seq": 0, "left_seq": 5}]}
    require_room_member("new", room, 5)
    with pytest.raises(TeamValidationError):
        require_room_member("new", room, 4)
    with pytest.raises(TeamValidationError):
        require_room_member("old", room)
    messages = [{"id": "early", "room_id": "room", "seq": 4},
                {"id": "late", "room_id": "room", "seq": 5},
                {"id": "private", "instance_id": "other"},
                {"id": "supplement", "instance_id": "new"},
                {"id": "result", "parent_task_id": "owned"}]
    context = project_context("new", {"id": "owned"}, messages, [room])
    assert [m["id"] for m in context["messages"]] == ["late", "supplement", "result"]


def test_whiteboard_requires_explicit_revision_and_valid_ops():
    with pytest.raises(TeamValidationError):
        validate_delivery({"action": "continue", "whiteboard": {"ops": []}})
    with pytest.raises(TeamValidationError):
        validate_delivery({"action": "continue", "whiteboard": {
            "base_rev": 1, "ops": [{"op": "replace", "find": "", "replace": "new"}]}})
    assert validate_delivery({"action": "continue", "whiteboard": {
        "base_rev": 1, "ops": [{"op": "append", "content": "new"}]}})["whiteboard"]["base_rev"] == 1


def test_malformed_enum_values_raise_validation_errors_instead_of_type_errors():
    value = definition()
    value["edges"][0]["type"] = []
    with pytest.raises(TeamValidationError):
        validate_team_definition(value)
    with pytest.raises(TeamValidationError):
        validate_delivery({"action": []})
    with pytest.raises(TeamValidationError):
        normalize_limits({"total_max_tokens": 10 ** 1000})


@pytest.mark.parametrize("first,second", [
    ("task", "task"), ("task", "room"), ("room", "task"), ("room", "room"),
])
def test_one_connection_per_unordered_pair_across_types_and_directions(first, second):
    source = definition()
    source["edges"] = [
        {"id": "first", "source": "a", "target": "b", "type": first},
        {"id": "second", "source": "b", "target": "a", "type": second},
    ]
    with pytest.raises(TeamValidationError) as error:
        validate_team_definition(source)
    assert error.value.errors == [{"message": "每对节点最多只能有一种连接，不能重复",
                                   "edge_ids": ["second"], "node_ids": ["b", "a"]}]


@pytest.mark.parametrize("single", [None, 0])
def test_empty_single_turn_limit_is_unlimited_and_does_not_remove_parent_cap(single):
    assert normalize_limits({"single_max_tokens": single})["single_max_tokens"] is None
    role = validate_role({"name": "审查", "default_budget": {"single_max_tokens": single}})
    assert role["default_budget"]["single_max_tokens"] is None
    assert normalize_budget({"single_max_tokens": single}) == {"single_max_tokens": None}
    with pytest.raises(TeamValidationError, match="不能提高"):
        narrow_capabilities({"budget": {"single_max_tokens": 40}},
                            {"budget": {"single_max_tokens": single}})
    assert narrow_capabilities({"budget": {"single_max_tokens": None}},
                               {"budget": {"single_max_tokens": 40}})["budget"]["single_max_tokens"] == 40


def test_missing_role_and_session_single_turn_limit_has_no_bounded_fallback():
    assert normalize_limits()["single_max_tokens"] is None
    assert "single_max_tokens" not in validate_role({"name": "审查"})["default_budget"]


def test_tools_catalog_is_finite_and_honors_server_enabled_tools(monkeypatch):
    from app import config
    from app.domain.team_tools import available_team_tools
    monkeypatch.setattr(config, "DSH_TOOLS", ("read", "web_search", "arbitrary_plugin"))
    catalog = available_team_tools()
    assert {tool["name"] for tool in catalog} == {"read", "web_search"}
    assert all(set(tool) == {"name", "label", "description"} for tool in catalog)
    assert validate_role({"name": "审查", "tools": ["read", "read"]})["tools"] == ["read"]
    for tools in (["write"], ["arbitrary_plugin"], ["subagent"], ["read", "unknown"]):
        with pytest.raises(TeamValidationError, match="服务器"):
            validate_role({"name": "审查", "tools": tools})
    assert narrow_capabilities({"tools": ["read"]})["tools"] == ["read"]
    with pytest.raises(TeamValidationError):
        narrow_capabilities({"tools": ["read"]}, {"tools": ["web_search"]})
    with pytest.raises(TeamValidationError, match="服务器"):
        narrow_capabilities({"tools": ["arbitrary_plugin"]})


def test_sources_are_preserved_for_roles_and_teams_and_reject_unsafe_urls():
    metadata = {"preset_key": "verified-team", "sources": [
        {"title": "官方说明", "url": "https://example.com/docs?version=1#tools"}]}
    role = validate_role({"name": "审查", **metadata})
    team = validate_team_definition({**definition(), **metadata})
    for record in (role, team):
        assert record["sources"] == metadata["sources"]
        assert record["preset_key"] == metadata["preset_key"]
    for url in ("javascript:alert(1)", "file:///private", "https://", "https://a.invalid:bad",
                "https://user:password@example.com/", "https://example.com/a\nb"):
        with pytest.raises(TeamValidationError, match="来源链接"):
            validate_role({"name": "审查", "sources": [{"title": "来源", "url": url}]})
