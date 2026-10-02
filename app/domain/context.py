"""Public conversation context and unchanged purpose-specific prompts."""

from .whiteboard import can_edit, is_proposer

def build_persona(runner, agent: dict) -> str:
    parts = [runner.shared_background or "（无共享背景）"]
    parts.append(f"\n\n你的名字是：{agent['name']}")
    parts.append(f"\n\n【你自己的角色设定】\n{agent['system_prompt'] or '（未设定，自然参与即可）'}")

    visible_others = []
    for other in runner.agents:
        if other["id"] == agent["id"]:
            continue
        vis = other.get("visibility") or []
        is_visible = (
            vis == "all"
            or (isinstance(vis, list) and ("all" in vis or agent["id"] in vis or agent["name"] in vis))
        )
        if is_visible:
            visible_others.append(other)

    if visible_others:
        parts.append("\n\n【你被告知的其他参与者的角色设定】")
        for other in visible_others:
            parts.append(f"- {other['name']}: {other['system_prompt'] or '（未设定）'}")

    return "\n".join(parts)


def build_system(runner, agent: dict) -> str:
    base = build_persona(runner, agent) + (
        "\n\n你正在参与一场多人实时群聊。请以你角色的口吻，用中文自然发言。"
    )
    can_end = is_proposer(runner, agent)
    can_wb = can_edit(runner, agent)

    fields = [
        '"speech"：字符串，你本轮要说的话（用中文，直接说话，不要加「某某说：」之类前缀）',
    ]
    if can_end:
        fields.append(
            '"propose_end"：布尔值。若你认为这场对话可以结束了就设为 true，'
            "这会发起一次全体投票，只有所有参与者都同意才会结束；不想结束就设为 false 或省略"
        )
    if can_wb:
        fields.append(
            '"whiteboard"：对象，用于对共享白板做增量修改；本轮不改就省略或设为 null。'
            '格式为 {"ops": [...]}，每个 op 可为：'
            '{"op":"append","content":"追加到白板末尾的内容"}、'
            '{"op":"replace","find":"白板中已有的原文片段","replace":"替换后的新内容"}、'
            '{"op":"prepend","content":"加到白板开头的内容"}'
        )
    base += (
        "\n\n最终交付请只输出一个 JSON 对象，不要输出任何多余文字，包含以下字段：\n"
        + "\n".join(f"- {f}" for f in fields)
    )
    if runner.harness is not None:
        base += (
            "\n\n在最终交付前，你可以按需使用已启用的工具查资料、读取文件或计算。"
            "不要为了使用工具而使用工具；引用外部资料时在 speech 中给出来源。"
            "上面的 JSON 约束仅针对最终交付，工具调用走工具协议。"
            "仅完成当前角色的本轮任务，发言调度、投票和共享白板由 KDS 管理。"
            "白板只能通过最终 JSON 修改，工具产物保存在自己的工作目录。"
        )
    if can_wb:
        fmt = "HTML" if runner.whiteboard_format == "html" else "Markdown"
        current = runner.whiteboard_content or "（当前白板为空）"
        base += (
            f"\n\n【共享白板】这是本次群聊要沉淀的最终产出物，格式为 {fmt}，"
            "请在合适时机通过 whiteboard 字段逐步完善它。当前白板内容如下：\n"
            f"---\n{current}\n---"
        )
    return base


def build_score_system(runner, agent: dict) -> str:
    return build_persona(runner, agent) + (
        "\n\n你正在参与一场多人实时群聊。现在需要你评估："
        "基于当前对话内容，你有多想发言。"
    )


def history(runner) -> list[dict]:
    return [
        {"role": "user", "content": f"{m['speaker']}: {m['content']}"}
        for m in runner.messages
    ]


def log_text(runner) -> str:
    return "\n".join(f"{m['speaker']}: {m['content']}" for m in runner.messages)


def build_vote_system(runner, agent: dict, question: str, options: list[str], votes_per_person: int) -> str:
    options_text = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
    return build_persona(runner, agent) + (
        "\n\n你正在参与群聊中的投票。请基于当前对话内容和你自己的角色立场投票。"
        "\n投票题目：\n" + question +
        "\n\n选项：\n" + options_text +
        f"\n\n你有 {votes_per_person} 票，可以对不同选项任意分配，也可以重复投给同一选项。"
        '请只输出 JSON 对象，格式：{"choices": ["1", "2"], "reason": "简短理由"}，'
        "choices 数组长度应等于你的票数，每项是选项编号。"
    )
