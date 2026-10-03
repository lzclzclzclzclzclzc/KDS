"""Pure rules for versioned teams, scoped work, and capability inheritance.

The canvas describes permissions; it is deliberately independent from the
LangGraph execution graph. No helper in this module performs a side effect.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from urllib.parse import urlsplit

from app.domain.team_tools import team_tool_allowlist


TERMINAL_TASK_STATES = frozenset({"succeeded", "failed", "cancelled"})
TASK_STATES = frozenset({"queued", "running", "waiting_children", "paused",
                         "cancelling"}) | TERMINAL_TASK_STATES
DELIVERY_ACTIONS = frozenset({"continue", "wait_children", "complete_task"})
DEFAULT_LIMITS = {
    "single_max_tokens": None,
    "total_max_tokens": 10000,
    "total_duration_seconds": None,
    "max_concurrency": 2,
    "max_processes": 4,
    "max_depth": 6,
    "max_instances": 32,
    "max_children": 8,
    "max_tasks": 128,
    "max_activations_per_task": 12,
    "max_discussion_turns": 6,
    "max_queue_length": 256,
    "summary_max_tokens": 300,
}


class TeamValidationError(ValueError):
    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or [{"message": message}]


def _object(value, label):
    if not isinstance(value, dict):
        raise TeamValidationError(f"{label}必须是对象")
    return value


def _string(value, label, *, default=None, required=False):
    if value is None and default is not None:
        value = default
    if not isinstance(value, str):
        raise TeamValidationError(f"{label}必须是字符串")
    value = value.strip()
    if required and not value:
        raise TeamValidationError(f"{label}不能为空")
    return value


def _positive(value, label, *, integer=True, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TeamValidationError(f"{label}必须是正{'整数' if integer else '数'}")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value <= 0 or (integer and int(value) != value):
        raise TeamValidationError(f"{label}必须是有限正{'整数' if integer else '数'}")
    return int(value) if integer else float(value)


def _quota(value, key):
    # Empty single-turn fields and the UI's zero both mean unlimited.
    if key == "single_max_tokens" and not isinstance(value, bool) and value == 0:
        value = None
    return _positive(value, key, integer=key != "total_duration_seconds",
                     nullable=key in {"single_max_tokens", "total_max_tokens", "total_duration_seconds"})


def _tools(value):
    if not isinstance(value, list) or any(not isinstance(t, str) or not t.strip() for t in value):
        raise TeamValidationError("工具权限必须是非空字符串数组")
    tools = list(dict.fromkeys(t.strip() for t in value))
    unknown = set(tools) - team_tool_allowlist()
    if unknown:
        raise TeamValidationError("工具未在服务器启用：" + "、".join(sorted(unknown)))
    return tools


def _source_metadata(payload):
    result = {}
    if "sources" in payload:
        sources = payload["sources"]
        if not isinstance(sources, list):
            raise TeamValidationError("来源必须是标题与链接的数组")
        result["sources"] = []
        for source in sources:
            _object(source, "来源")
            title = _string(source.get("title"), "来源标题", required=True)
            url = _string(source.get("url"), "来源链接", required=True)
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                valid = valid and not parsed.username and not parsed.password and not any(c.isspace() for c in url)
                parsed.port  # Reject malformed ports as well as malformed hosts.
            except ValueError:
                valid = False
            if not valid:
                raise TeamValidationError("来源链接必须是有效的 HTTP 或 HTTPS 地址")
            result["sources"].append({"title": title, "url": url})
    if payload.get("preset_key") is not None:
        result["preset_key"] = _string(payload["preset_key"], "预设标识", required=True)
    return result


def normalize_limits(payload=None, require_finite=True):
    """Validate quotas without accepting bools or non-finite values.

    Null/zero single-turn quota means unlimited. Session totals retain their
    existing null semantics; other quotas use bounded defaults.
    """
    payload = _object({} if payload is None else payload, "运行上限")
    values = dict(DEFAULT_LIMITS)
    for key in DEFAULT_LIMITS:
        if key in payload:
            values[key] = _quota(payload[key], key)
    if require_finite and all(values[k] is None for k in
                              ("total_max_tokens", "total_duration_seconds")):
        raise TeamValidationError("总输出额度和总时长至少设置一个有限上限")
    return values


def normalize_budget(payload):
    """Normalize only explicit task/role grants; do not inject session defaults."""
    payload = _object(payload, "任务预算")
    result = {}
    for key, value in payload.items():
        if key not in DEFAULT_LIMITS:
            raise TeamValidationError(f"不支持的预算：{key}")
        result[key] = _quota(value, key)
    return result


def validate_role(payload):
    payload = _object(payload, "角色")
    role = {
        "name": _string(payload.get("name"), "角色名称", required=True),
        "system_prompt": _string(payload.get("system_prompt"), "角色提示词", default=""),
        "model_config_id": _string(payload.get("model_config_id"), "模型配置", default="default", required=True),
        "tools": [],
        "default_budget": {},
        "visibility": [],
    }
    role["tools"] = _tools(payload.get("tools", []))
    visibility=payload.get("visibility",[])
    if visibility != "all" and (not isinstance(visibility,list) or
            any(not isinstance(value,str) or not value.strip() for value in visibility)):
        raise TeamValidationError("角色设定可见性必须是 all 或实例/节点 ID 数组")
    role["visibility"] = visibility if visibility == "all" else list(dict.fromkeys(visibility))
    role["default_budget"] = normalize_budget(payload.get("default_budget", {}))
    if "description" in payload:
        role["description"] = _string(payload["description"], "角色说明", default="")
    role.update(_source_metadata(payload))
    return role


def role_equivalence_key(role):
    """Compare complete role definitions while keeping entity versions separate.

    Read-side comparison does not revalidate stored tools against today's
    server catalog. A disabled tool remains a meaningful configuration change.
    """
    metadata = {'id','version','archived','created_at','updated_at','equivalence_key','preset_role_key'}
    value = {key:deepcopy(item) for key,item in role.items() if key not in metadata}
    value.setdefault('system_prompt','')
    value.setdefault('model_config_id','default')
    value['tools'] = sorted(set(value.get('tools',[])))
    visibility = value.get('visibility',[])
    value['visibility'] = 'all' if visibility=='all' or 'all' in visibility else sorted(set(visibility))
    # Missing/null/zero single-turn grants all inherit the same effective
    # parent/session ceiling. Do not freeze an implicit unlimited cap.
    value['default_budget'] = {key:item for key,item in value.get('default_budget',{}).items()
                               if item is not None and not (key=='single_max_tokens' and item==0)}
    encoded = json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def validate_team_definition(payload, roles=None):
    """Validate a directed forest and separate undirected room components.

    Each unordered node pair has one edge at most, regardless of type or
    direction. Mixed cycles remain legal; only task edges define parent trees.
    """
    payload = _object(payload, "团队配置")
    # An exported file may carry a version envelope.
    if "definition" in payload:
        payload = _object(payload["definition"], "团队定义")
    raw_nodes, raw_edges = payload.get("nodes", []), payload.get("edges", [])
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise TeamValidationError("节点和连线必须是数组")
    errors, nodes, ids = [], [], set()
    if not raw_nodes:
        errors.append({"message": "团队至少需要一个节点"})
    for index, node in enumerate(raw_nodes):
        if not isinstance(node, dict):
            errors.append({"message": f"第 {index + 1} 个节点必须是对象"})
            continue
        node_id = node.get("id", node.get("node_id"))
        if not isinstance(node_id, str) or not node_id.strip() or node_id in ids:
            errors.append({"message": "节点 ID 必须是互不重复的非空字符串",
                           "node_ids": [node_id] if isinstance(node_id, str) else []})
            continue
        ids.add(node_id)
        try:
            role_id = _string(node.get("role_id"), "角色 ID", required=True)
            role_version = _positive(node.get("role_version", 1), "角色版本")
            if roles is not None:
                role = roles.get(role_id) if isinstance(roles, dict) else next(
                    (r for r in roles if r.get("id") == role_id), None)
                if role is None:
                    raise TeamValidationError("引用的角色不存在")
                if role.get("archived"):
                    raise TeamValidationError("不能使用已归档角色创建新配置")
            position = _object(node.get("position") or {"x": node.get("x", 0), "y": node.get("y", 0)}, "节点位置")
            clean_position = {}
            for axis in ("x", "y"):
                coordinate = position.get(axis, 0)
                if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)) or not math.isfinite(coordinate):
                    raise TeamValidationError("节点位置必须是有限数字")
                clean_position[axis] = float(coordinate)
            clean = {
                "id": node_id,
                "name": _string(node.get("name"), "节点名称", default=node_id, required=True),
                "role_id": role_id, "role_version": role_version,
                "position": clean_position,
                "prompt_supplement": _string(node.get("prompt_supplement"), "提示词补充", default=""),
            }
            if "capabilities" in node:
                clean["capabilities"] = deepcopy(_object(node["capabilities"], "节点能力"))
            nodes.append(clean)
        except TeamValidationError as exc:
            errors.append({"message": str(exc), "node_ids": [node_id]})
    edges, edge_ids, pairs, parents = [], set(), set(), {}
    children = {node_id: [] for node_id in ids}
    neighbors = {node_id: set() for node_id in ids}
    for index, edge in enumerate(raw_edges):
        if not isinstance(edge, dict):
            errors.append({"message": f"第 {index + 1} 条连线必须是对象"})
            continue
        edge_id = edge.get("id") or f"edge-{index + 1}"
        source, target, kind = edge.get("source"), edge.get("target"), edge.get("type")
        issue = None
        if not isinstance(edge_id, str) or edge_id in edge_ids:
            issue = "连线 ID 必须是互不重复的字符串"
        elif not isinstance(kind, str) or kind not in {"task", "room"}:
            issue = "连线类型只能是 task 或 room"
        elif not isinstance(source, str) or not isinstance(target, str) or source not in ids or target not in ids:
            issue = "连线端点不存在"
        elif source == target:
            issue = "节点不能连接自身"
        else:
            pair = tuple(sorted((source, target)))
            if pair in pairs:
                issue = "每对节点最多只能有一种连接，不能重复"
            elif kind == "task" and target in parents:
                issue = "每个节点最多只能有一个父节点"
            else:
                pairs.add(pair)
                if kind == "task":
                    parents[target] = source
                    children[source].append(target)
                else:
                    neighbors[source].add(target)
                    neighbors[target].add(source)
        edge_ids.add(edge_id) if isinstance(edge_id, str) else None
        if issue:
            errors.append({"message": issue, "edge_ids": [edge_id],
                           "node_ids": [n for n in (source, target) if isinstance(n, str)]})
        else:
            edges.append({"id": edge_id, "source": source, "target": target, "type": kind})
    # Iterative parent walking avoids Python recursion limits for imported files.
    resolved = set()
    for start in sorted(ids):
        path, seen, current = [], set(), start
        while current is not None and current not in resolved:
            if current in seen:
                cycle = path[path.index(current):]
                errors.append({"message": "父子任务连线不能形成有向环", "node_ids": cycle})
                break
            seen.add(current)
            path.append(current)
            current = parents.get(current)
        resolved.update(path)
    if errors:
        raise TeamValidationError("团队配置校验失败", errors)
    roots = sorted(ids - set(parents))
    rooms, visited = [], set()
    for start in sorted(ids):
        if start in visited:
            continue
        pending, component = [start], []
        while pending:
            node_id = pending.pop()
            if node_id in visited:
                continue
            visited.add(node_id)
            component.append(node_id)
            pending.extend(sorted(neighbors[node_id] - visited, reverse=True))
        if len(component) > 1:
            component.sort()
            # Deterministic IDs are preview identities, not runtime room IDs.
            rooms.append({"id": f"room-{len(rooms) + 1}", "node_ids": component})
    definition = {
        "name": _string(payload.get("name"), "团队名称", default="未命名团队", required=True),
        "shared_background": _string(payload.get("shared_background"), "共享背景", default=""),
        "nodes": nodes, "edges": edges, "roots": roots, "rooms": rooms,
    }
    for key in ("whiteboard_enabled", "whiteboard_format", "whiteboard_editors", "viewport"):
        if key in payload:
            definition[key] = deepcopy(payload[key])
    if "limits" in payload:
        definition["limits"] = normalize_limits(payload["limits"])
    definition.update(_source_metadata(payload))
    return definition


def validate_entry_nodes(definition, entry_node_ids):
    if not isinstance(entry_node_ids, list) or not entry_node_ids:
        raise TeamValidationError("至少选择一个入口根节点")
    if any(not isinstance(node_id, str) for node_id in entry_node_ids) or len(set(entry_node_ids)) != len(entry_node_ids):
        raise TeamValidationError("入口节点必须是互不重复的节点 ID")
    roots = definition.get("roots")
    if roots is None:
        children = {e["target"] for e in definition["edges"] if e["type"] == "task"}
        roots = {n["id"] for n in definition["nodes"]} - children
    if not set(entry_node_ids).issubset(roots):
        raise TeamValidationError("入口只能选择团队中的根节点")
    return list(entry_node_ids)


def narrow_capabilities(parent, requested=None):
    """A child can remove capabilities but cannot acquire a new grant.

    Missing tools inherits; an explicit empty list denies every tool. Model
    selection and filesystem grants are capabilities, never prompt requests.
    """
    parent = _object(parent, "父任务能力")
    requested = _object({} if requested is None else requested, "子任务能力")
    result = deepcopy(parent)
    if "tools" in parent:
        result["tools"] = _tools(parent["tools"])
    for key in ("tools", "model_config_ids", "file_roots"):
        if key not in requested:
            continue
        allowed, proposed = parent.get(key, []), requested[key]
        if not isinstance(allowed, list) or not isinstance(proposed, list) or any(not isinstance(v, str) for v in proposed):
            raise TeamValidationError(f"{key} 必须是字符串数组")
        if not set(proposed).issubset(allowed):
            raise TeamValidationError(f"子任务不能扩大 {key} 权限")
        result[key] = _tools(proposed) if key == "tools" else list(dict.fromkeys(proposed))
    if "model_config_id" in requested:
        proposed = requested["model_config_id"]
        allowed = parent.get("model_config_ids", [parent.get("model_config_id", "default")])
        if proposed not in allowed:
            raise TeamValidationError("子任务不能切换到未授权模型配置")
        result["model_config_id"] = proposed
    for container in ("budget", "default_budget", "limits"):
        if container not in requested:
            continue
        baseline = normalize_budget(parent.get(container) or {})
        proposed = _object(requested[container], "子任务预算")
        narrowed = deepcopy(baseline)
        for key, value in proposed.items():
            if key not in DEFAULT_LIMITS:
                raise TeamValidationError(f"不支持的预算：{key}")
            value = _quota(value, key)
            ceiling = baseline.get(key)
            if ceiling is not None and (value is None or value > ceiling):
                raise TeamValidationError(f"子任务不能提高 {key} 上限")
            narrowed[key] = value
        result[container] = narrowed
    # Explicit single-level budget fields use the same rules.
    for key in DEFAULT_LIMITS.keys() & requested.keys():
        value = _quota(requested[key], key)
        ceiling = _quota(parent[key], key) if key in parent else None
        if ceiling is not None and (value is None or value > ceiling):
            raise TeamValidationError(f"子任务不能提高 {key} 上限")
        result[key] = value
    return result


def require_direct_child(parent_id, child_id, edges):
    if parent_id == child_id or not any(e.get("type") == "task" and
            e.get("source") == parent_id and e.get("target") == child_id for e in edges):
        raise TeamValidationError("只能向自己的直接子实例派发任务")


def require_owned_task(parent_task_id, task):
    if task.get("parent_task_id") != parent_task_id:
        raise TeamValidationError("只能操作自己创建的直接子任务")


def require_room_member(instance_id, room, message_seq=None):
    members = room.get("members", room.get("instance_ids", []))
    for member in members:
        if member == instance_id:
            return
        if isinstance(member, dict) and member.get("instance_id") == instance_id:
            start = member.get("joined_seq", member.get("start_seq", 0))
            end = member.get("left_seq", member.get("end_seq"))
            if (message_seq is None and end is None) or (message_seq is not None and
                    message_seq >= start and (end is None or message_seq < end)):
                return
    raise TeamValidationError("实例无权读取或发送此房间消息")


def validate_delivery(payload, child_tasks=(), task_id=None):
    payload = _object(payload, "任务交付")
    action = payload.get("action")
    if not isinstance(action, str) or action not in DELIVERY_ACTIONS:
        raise TeamValidationError("交付动作只能是 continue、wait_children 或 complete_task")
    tasks = list(child_tasks.values()) if isinstance(child_tasks, dict) else list(child_tasks)
    result = {"action": action,
              "speech": _string(payload.get("speech"), "阶段说明", default=""),
              "result": deepcopy(payload.get("result", payload.get("speech", "")))}
    if action == "complete_task" and any(t.get("status") not in TERMINAL_TASK_STATES for t in tasks):
        raise TeamValidationError("仍有存活子任务，请先等待或取消后再完成当前任务")
    if action == "wait_children":
        wait = payload.get("wait") or {"task_ids": payload.get("wait_for", payload.get("child_task_ids", [])),
                                       "mode": payload.get("wait_mode", "all")}
        _object(wait, "等待条件")
        task_ids = wait.get("task_ids", wait.get("child_task_ids", []))
        mode = wait.get("mode", "all")
        if not isinstance(mode, str) or mode not in {"all", "any_success"}:
            raise TeamValidationError("等待方式只能是 all 或 any_success")
        if not isinstance(task_ids, list) or not task_ids or any(not isinstance(t, str) for t in task_ids) or len(set(task_ids)) != len(task_ids):
            raise TeamValidationError("等待集合必须包含互不重复的直接子任务 ID")
        owned = {t.get("id", t.get("task_id")): t for t in tasks}
        if not set(task_ids).issubset(owned):
            raise TeamValidationError("不能等待未由当前任务创建的子任务")
        if task_id is not None:
            for child_id in task_ids:
                require_owned_task(task_id, owned[child_id])
        result["wait"] = {"task_ids": list(task_ids), "mode": mode}
        if "timeout_seconds" in wait:
            result["wait"]["timeout_seconds"] = _positive(
                wait["timeout_seconds"], "等待时限", integer=False)
    if payload.get("whiteboard") is not None:
        whiteboard = _object(payload["whiteboard"], "产出白板修改")
        base_rev = whiteboard.get("base_rev")
        if isinstance(base_rev, bool) or not isinstance(base_rev, int) or base_rev < 0:
            raise TeamValidationError("白板修改必须包含非负整数 base_rev")
        ops = whiteboard.get("ops")
        if not isinstance(ops, list):
            raise TeamValidationError("白板 ops 必须是数组")
        for op in ops:
            _object(op, "白板操作")
            if not isinstance(op.get("op"), str) or op["op"] not in {"append", "prepend", "replace", "set"}:
                raise TeamValidationError("白板操作类型无效")
            fields = ("find", "replace") if op["op"] == "replace" else ("content",)
            for field in fields:
                _string(op.get(field), f"白板 {field}", required=field == "find")
        result["whiteboard"] = deepcopy(whiteboard)
    return result


def wait_satisfied(wait_spec, tasks):
    indexed = tasks if isinstance(tasks, dict) else {t.get("id", t.get("task_id")): t for t in tasks}
    ids = wait_spec.get("task_ids", [])
    if not ids:
        return False
    selected = [indexed.get(task_id) for task_id in ids]
    if any(t is None for t in selected):
        return False
    if wait_spec.get("mode", "all") == "any_success" and any(t.get("status") == "succeeded" for t in selected):
        return True
    return all(t.get("status") in TERMINAL_TASK_STATES for t in selected)


def project_context(instance_id, task, messages, rooms=()):
    """Select authorized committed inputs without copying another instance's history."""
    visible = []
    room_index = {r["id"]: r for r in rooms}
    for message in messages:
        if message.get("instance_id") == instance_id or instance_id in message.get("recipient_ids", []):
            visible.append(deepcopy(message))
        elif message.get("parent_task_id") == task.get("id"):
            visible.append(deepcopy(message))
        elif message.get("room_id") in room_index:
            try:
                require_room_member(instance_id, room_index[message["room_id"]], message.get("seq", 0))
            except TeamValidationError:
                continue
            visible.append(deepcopy(message))
    return {"task": deepcopy(task), "messages": visible}
