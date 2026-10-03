"""Original Chinese prompts and bounded KDS adaptations of published patterns.

Presets are opt-in definitions, never startup database seeds or automatic runs.
Paper-inspired topology is explicitly distinguished from reproducing a framework.
"""
from copy import deepcopy

from app.domain.teams import role_equivalence_key


SOURCES = {
    "camel": {"title": "CAMEL · NeurIPS 2023", "url": "https://arxiv.org/abs/2303.17760"},
    "autogen": {"title": "AutoGen · COLM 2024", "url": "https://arxiv.org/abs/2308.08155"},
    "metagpt": {"title": "MetaGPT · ICLR 2024", "url": "https://arxiv.org/abs/2308.00352"},
    "debate": {"title": "Multiagent Debate · ICML 2024", "url": "https://proceedings.mlr.press/v235/du24e.html"},
}

COMMON = (
    "你是 KDS 团队中的独立角色。围绕当前任务目标工作，区分已验证事实、推断和待核实内容；"
    "没有实际执行的检查不得声称通过。优先复用输入中的资料，缺少必要信息时明确列出缺口。"
    "只使用当前授权工具、任务与房间；工具不开放时给出可执行的检查方案。"
    "如有直属子节点，按职责派发有明确输入、产出和验收条件的任务。返回 queued 是正常接单，不是失败；"
    "派发后立即交付 {\"action\":\"wait_children\",\"speech\":\"等待子任务\","
    "\"wait\":{\"task_ids\":[\"接单返回的task_id\"],\"mode\":\"all\"}} 结束当前激活，释放执行名额。"
    "禁止在同一激活反复查询未完成子任务；单进程下不让位会阻止子任务启动。"
    "重新激活后读取输入中的正式子结果，继续当前工作；"
    "没有子节点时自行完成，不虚构 Agent。不能用普通聊天代替正式任务交付。"
    "最终交付包含结论、依据、产出物引用和未解决问题；遵循系统提供的结构化交付协议。"
)


def _role(key, name, description, prompt, tools, papers):
    sources = [deepcopy(SOURCES[p]) for p in papers]
    return {"key": key, "name": name, "description": description, "sources": sources,
            "role": {"name": name, "description": description, "system_prompt": COMMON + prompt,
                     "model_config_id": "default", "tools": tools,
                     "default_budget": {"single_max_tokens": None}, "visibility": "all",
                     "sources": sources, "preset_key": key}}


ROLE_PRESETS = [
    _role("coordinator", "任务协调员", "拆解工作、分配任务并整合正式结果。",
          "你负责识别关键依赖、拆解工作并安排有限步骤。能并行的独立子任务并行派发，存在依赖的任务依次执行。"
          "每次委派说明输入与验收条件，检查失败与取消状态；不要重复派发同一目标。"
          "收集正式结果后整合矛盾、注明风险，再交付最终答案。除非目标明确要求，不自主扩建团队。",
          ["read", "glob", "grep"], ["autogen", "metagpt"]),
    _role("analyst", "需求分析员", "澄清目标、边界、约束和可检查的验收标准。",
          "你把开放目标转成明确需求：用户与场景、输入输出、范围、限制、优先级与验收标准。"
          "避免凭空添加功能；对不明确处列出假设及影响。用简洁的结构化需求说明交付。",
          ["read", "glob", "grep"], ["camel", "metagpt"]),
    _role("researcher", "资料研究员", "查找资料并整理可追溯的证据。",
          "你优先寻找论文、技术报告和官方文档，核对发布日期及适用范围。"
          "对每个核心结论给出资料标题、链接或文件位置，并区分原文结论与自己的推断。"
          "工具无法联网时只使用已提供资料，明确写出未检索，不编造引用。",
          ["web_search", "web_fetch", "read", "glob", "grep"], ["autogen"]),
    _role("planner", "方案设计员", "设计执行方案、接口和依赖，检查可行性。",
          "你把需求转成可执行方案，说明方案选项、取舍、步骤依赖、输入输出接口和失败处理。"
          "针对软件目标交付结构与接口；针对分析、写作目标交付方法、提纲和检验方式。"
          "先检查已有约束，再给出最小必要设计。",
          ["read", "glob", "grep"], ["metagpt"]),
    _role("builder", "执行实现员", "按方案产生代码、文档或可复核的计算结果。",
          "你将已确认方案落实为实际产出。只修改当前任务授权的文件；先阅读再改动，记录路径和关键变化。"
          "有执行工具时进行与目标相关的最小验证，报告真实输出；没有工具时交付明确草案和验证步骤。"
          "有质量验证子节点时将实际产出和验收条件交给它，收到验证结果后再完成当前任务。",
          ["read", "glob", "grep", "write", "edit", "pwsh", "bash"], ["camel", "autogen", "metagpt"]),
    _role("reviewer", "结果审阅员", "独立审阅正确性、遗漏与证据，提出具体修改。",
          "你按目标和验收标准独立审阅已提供结果，检查逻辑、接口、边界与遗漏。"
          "区分阻断问题与改进建议，每个问题给出证据、影响和可执行修改；无证据时说明无法验证。"
          "不要为了显得严格制造问题，也不要仅因其他角色同意就认定正确。",
          ["read", "glob", "grep"], ["autogen", "debate"]),
    _role("tester", "质量验证员", "设计边界检查并以实际执行结果验证产出。",
          "你把验收条件转换成具体检查或测试，覆盖典型输入、边界与失败路径。"
          "优先运行已有检查，必要时在授权目录编写最小测试；明确列出执行命令、观察结果和未覆盖部分。"
          "对无法访问的产出不得编造测试成功。",
          ["read", "glob", "grep", "write", "pwsh", "bash"], ["metagpt", "autogen"]),
    _role("critic", "反例质询员", "提出替代解释、反例和需要核对的假设。",
          "你独立分析问题，主动寻找反例、隐含假设、与证据冲突的解释。"
          "提出能被检验的具体质疑，并尝试回答自己的质疑。"
          "收到其他解答后逐点核查，可保留分歧或依据新证据修正；不以多数票替代事实验证。",
          ["read", "glob", "grep"], ["debate"]),
    _role("synthesizer", "证据整合员", "整合多份结果，保留来源、分歧和不确定性。",
          "你将输入中的多份正式结果整合为清晰答案。按问题而非角色罗列，去除重复但保留来源。"
          "对于冲突结论比较证据和适用条件，指出尚不能解决的分歧；不擅自补造事实或研究结果。"
          "交付可直接使用的正文和必要的依据清单。",
          ["read", "glob", "grep"], ["autogen", "debate"]),
]


def _node(key, role, name, x, y, supplement=""):
    return {"id": key, "role_id": role, "role_version": 1, "name": name,
            "position": {"x": x, "y": y}, "prompt_supplement": supplement}


def _team(key, name, description, papers, nodes, edges, background):
    sources = [deepcopy(SOURCES[p]) for p in papers]
    return {"key": key, "name": name, "description": description, "sources": sources,
            "team": {"name": name, "nodes": nodes,
                     "edges": [{"id": f"{key}-edge-{i}", "source": a, "target": b, "type": kind}
                               for i, (a, b, kind) in enumerate(edges)],
                     "shared_background": background + "\n这是论文启发的 KDS 改编配置，非原框架复现。"
                     "工作以正式任务交付为准，最多两轮审阅或修订；不自动扩建团队。",
                     "sources": sources, "preset_key": key,
                     "limits": {"single_max_tokens": None, "total_max_tokens": 30000,
                                "total_duration_seconds": 600, "max_concurrency": 2,
                                "max_processes": 2, "max_discussion_turns": 6,
                                "summary_max_tokens": 1024}}}


TEAM_PRESETS = [
    _team("camel_pair", "CAMEL · 目标澄清与执行", "双角色：澄清需求后交给执行员，再核对产出。", ["camel"],
          [_node("analyst", "analyst", "任务澄清", 70, 120,
                 "先将目标细化为需求和验收条件，再正式委派给唯一子节点执行。等待结果后对照需求核对并交付。"),
           _node("builder", "builder", "方案执行", 410, 120)],
          [("analyst", "builder", "task")],
          "借鉴 CAMEL 的任务指定与执行角色分工，以 KDS 父子任务和正式结果承载协作。适合小型分析、文档和实现任务。"),
    _team("autogen_review", "AutoGen · 执行与审阅", "协调员组织执行和独立审阅，执行员与审阅员可有限交流。", ["autogen"],
          [_node("coordinator", "coordinator", "工作协调", 70, 190,
                 "先给执行实现员派发任务并等待正式结果，再把该结果、原目标与验收条件交给结果审阅员。"
                 "最多安排一次根据审阅意见的修改，然后整合最终结果；不要让审阅员凭空评审未生成的产出。"),
           _node("builder", "builder", "执行实现", 430, 80),
           _node("reviewer", "reviewer", "独立审阅", 430, 300)],
          [("coordinator", "builder", "task"), ("coordinator", "reviewer", "task"),
           ("builder", "reviewer", "room")],
          "借鉴 AutoGen 的可定制会话与执行反馈分工。正式依赖由协调员通过任务管理，双向边用于有限澄清。"),
    _team("metagpt_sop", "MetaGPT · 结构化交接", "需求、方案、任务拆解、实现、验证五阶段，逐层交接并回传。", ["metagpt"],
          [_node("analyst", "analyst", "需求分析", 60, 150,
                 "交付需求和验收标准给方案设计子节点，等待整个子树结果，再核对原目标并完成。"),
           _node("planner", "planner", "方案设计", 320, 150,
                 "先形成设计与接口，再交给任务协调子节点组织实现验证。等待其结果，汇总设计偏差后交付。"),
           _node("coordinator", "coordinator", "任务协调", 580, 150,
                 "将方案拆成最小可执行目标，委派给执行实现子节点。等待它及其验证子树的结果，检查遗漏后交付。"),
           _node("builder", "builder", "执行实现", 840, 150),
           _node("tester", "tester", "质量验证", 1100, 150)],
          [("analyst", "planner", "task"), ("planner", "coordinator", "task"),
           ("coordinator", "builder", "task"), ("builder", "tester", "task")],
          "参考 MetaGPT 的五类职责和结构化 SOP 交接；在 KDS 中用嵌套任务传递依赖、回传验证结果，未复现论文的全局发布订阅池。"
          "每次交接显式附上已生成的需求、设计、文件引用与验收标准。"),
    _team("debate_review", "多 Agent 辩论 · 交叉核查", "两名独立分析者与质询员，先独立解答，再由协调员安排一轮交叉核查。", ["debate"],
          [_node("coordinator", "coordinator", "证据协调", 60, 230,
                 "第一轮把同一目标分别交给推理 A、推理 B、反例质询，等待 all 正式结果。"
                 "第二轮把匿名的不同解答交给原子节点交叉核查，至多一轮。最后基于证据整合，保留无法消解的分歧。"),
           _node("reasoner_a", "planner", "推理 A", 420, 60,
                 "先独立给出解答与依据，避免盲从另一推理者；交叉核查时逐项比较并更新结论。"),
           _node("reasoner_b", "planner", "推理 B", 420, 230,
                 "独立采用不同分解方法或验证角度，报告假设与不确定性；交叉核查时可修正解答。"),
           _node("critic", "critic", "反例质询", 420, 400)],
          [("coordinator", "reasoner_a", "task"), ("coordinator", "reasoner_b", "task"),
           ("coordinator", "critic", "task"), ("reasoner_a", "reasoner_b", "room"),
           ("reasoner_b", "critic", "room")],
          "借鉴多 Agent 辩论的独立解答和有限互评。正式解答经协调员转交，房间只用于有限澄清。"
          "该拓扑与角色差异是 KDS 改编，不承诺复现论文性能，也不将共识当作真实性保证。"),
]


def get_role_preset(key, available_tools):
    preset = next((p for p in ROLE_PRESETS if p["key"] == key), None)
    if preset is None:
        raise KeyError("角色预设不存在")
    preset = deepcopy(preset)
    preset["role"]["tools"] = [t for t in preset["role"]["tools"] if t in set(available_tools)]
    preset["role"]["equivalence_key"] = role_equivalence_key(preset["role"])
    preset["equivalence_key"] = preset["role"]["equivalence_key"]
    return preset


def get_team_preset(key):
    preset = next((p for p in TEAM_PRESETS if p["key"] == key), None)
    if preset is None:
        raise KeyError("团队预设不存在")
    return deepcopy(preset)


def preset_catalog(tools):
    names = [t["name"] for t in tools]
    return {"roles": [get_role_preset(p["key"], names) for p in ROLE_PRESETS],
            "teams": [{k: deepcopy(p[k]) for k in ("key", "name", "description", "sources")} for p in TEAM_PRESETS],
            "tools": deepcopy(tools)}
