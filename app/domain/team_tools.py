"""Server-selected native tools shared by team validation and preset metadata."""
from app import config


TEAM_TOOL_CATALOG = (
    {"name": "web_search", "label": "网页搜索", "description": "搜索公开网页中的资料。"},
    {"name": "web_fetch", "label": "读取网页", "description": "读取指定网页的公开内容。"},
    {"name": "read", "label": "读取文件", "description": "读取运行工作目录内允许访问的文件。"},
    {"name": "glob", "label": "查找文件", "description": "按文件名模式查找允许访问的文件。"},
    {"name": "grep", "label": "搜索文件内容", "description": "在允许访问的文件中搜索文本。"},
    {"name": "write", "label": "写入文件", "description": "创建或覆盖允许写入的工作文件。"},
    {"name": "edit", "label": "修改文件", "description": "修改允许写入的工作文件。"},
    {"name": "pwsh", "label": "PowerShell", "description": "在运行时授权范围内执行 PowerShell 命令。"},
    {"name": "bash", "label": "Bash", "description": "在运行时授权范围内执行 Bash 命令。"},
)


def available_team_tools():
    """Unknown environment names never expand the fixed team tool catalog."""
    enabled = set(config.DSH_TOOLS)
    return [dict(tool) for tool in TEAM_TOOL_CATALOG if tool["name"] in enabled]


def team_tool_allowlist():
    return frozenset(tool["name"] for tool in available_team_tools())
