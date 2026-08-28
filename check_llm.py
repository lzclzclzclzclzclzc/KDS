"""LLM 连接测试脚本。

用途：在真正启动群聊前，快速确认 .env 里的 LLM 配置能否连通。
它会读取 app.config 的配置、初始化一个真实（非 mock）的 LLMClient，
向配置的模型发一次最小请求，并打印结果与 token 用量。

用法：
    python check_llm.py
    python check_llm.py --prompt "用一句话介绍你自己"

退出码：0 表示连通成功，非 0 表示失败（便于脚本/CI 判断）。
"""

import argparse
import sys
import time

# Windows 控制台默认可能是 GBK/cp1252，输出中文与 emoji 会报错，先切到 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import config
from app.llm import LLMClient


def _mask(secret: str) -> str:
    """脱敏显示 API key，只保留头尾少量字符。"""
    if not secret:
        return "(空)"
    if len(secret) <= 8:
        return secret[0] + "***"
    return f"{secret[:4]}...{secret[-4:]}"


def main() -> int:
    parser = argparse.ArgumentParser(description="测试 LLM 连接是否可用")
    parser.add_argument(
        "--prompt",
        default="请回复两个字：连通。",
        help="发送给模型的测试提示词",
    )
    args = parser.parse_args()

    print("=" * 56)
    print("LLM 连接测试")
    print("=" * 56)
    print(f"Base URL   : {config.LLM_BASE_URL}")
    print(f"Model      : {config.LLM_MODEL}")
    print(f"API Key    : {_mask(config.LLM_API_KEY)}")
    print(f"Timeout    : {config.LLM_TIMEOUT}s")
    print(f"LLM_MOCK   : {config.LLM_MOCK}")
    print("-" * 56)

    if config.LLM_MOCK:
        print("⚠️  当前 LLM_MOCK=true，这只会测试离线 mock，不会真正连接。")
        print("    如需测试真实接口，请在 .env 中设置 LLM_MOCK=false。")

    # 强制使用真实客户端（除非环境本就是 mock）——这里显式传 mock 值，
    # 让脚本行为跟随配置：mock 时验证 mock 路径，否则验证真实连接。
    try:
        client = LLMClient(mock=config.LLM_MOCK)
    except Exception as exc:  # 通常是缺少 API key
        print(f"\n❌ 客户端初始化失败：{exc}")
        return 1

    # 走 agent_turn，这是引擎真实使用的结构化 JSON 回合路径，最能代表实际用法。
    system = (
        "你是一个测试助手。请只输出一个 JSON 对象，格式："
        '{"speech": "你的回复"}。'
    )
    history = [{"role": "user", "content": args.prompt}]

    print(f"\n发送测试请求：{args.prompt!r}")
    start = time.time()
    try:
        turn, usage = client.agent_turn("测试", system, history, max_tokens=64)
        content = turn.get("speech", "")
    except Exception as exc:
        elapsed = time.time() - start
        print(f"\n❌ 请求失败（耗时 {elapsed:.2f}s）：")
        print(f"   {type(exc).__name__}: {exc}")
        print("\n排查建议：")
        print("  - 检查 LLM_BASE_URL 是否正确、可访问（含 /v1 后缀）")
        print("  - 检查 LLM_API_KEY 是否有效、未过期")
        print("  - 检查 LLM_MODEL 名称是否为该服务支持的模型")
        print("  - 检查网络/代理是否放行该地址")
        return 1

    elapsed = time.time() - start
    print(f"\n✅ 连接成功（耗时 {elapsed:.2f}s）")
    print("-" * 56)
    print("模型回复：")
    print(content or "(空回复)")
    print("-" * 56)
    print(
        "Token 用量："
        f"prompt={usage.get('prompt_tokens')} "
        f"completion={usage.get('completion_tokens')} "
        f"total={usage.get('total_tokens')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
