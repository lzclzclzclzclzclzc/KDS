"""Bounded, cancellable JSON-mode formatting; never executes tools."""
import asyncio
import time
from dataclasses import dataclass


class RepairCancelled(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass
class RepairReply:
    content: str
    finish_reason: str
    usage: dict


def request_json_repair(*, api_key, base_url, model, messages, max_tokens,
                        deadline, should_stop, client_factory=None):
    """No SDK retries or non-JSON fallback; caller owns attempts and accounting."""
    async def request():
        def check_stop():
            reason = should_stop()
            if reason:
                raise RepairCancelled(reason)
            if time.monotonic() >= deadline:
                raise RepairCancelled("timeout")

        check_stop()
        factory = client_factory
        if factory is None:
            from openai import AsyncOpenAI
            factory = AsyncOpenAI
            check_stop()
        async with factory(api_key=api_key, base_url=base_url, max_retries=0,
                           timeout=max(0.1, deadline - time.monotonic())) as client:
            task = asyncio.create_task(client.chat.completions.create(
                model=model, messages=messages, max_tokens=max_tokens, temperature=0,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            ))
            try:
                while True:
                    done, _ = await asyncio.wait({task}, timeout=0.1)
                    if done:
                        response = task.result()
                        usage = response.usage
                        choice = response.choices[0] if response.choices else None
                        # Return settled usage even if there is no usable text.
                        return RepairReply(
                            content=(choice.message.content or "") if choice else "",
                            finish_reason=choice.finish_reason if choice else "missing",
                            usage={"prompt_tokens": usage.prompt_tokens if usage else 0,
                                   "completion_tokens": usage.completion_tokens if usage else 0,
                                   "total_tokens": usage.total_tokens if usage else 0},
                        )
                    check_stop()
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    return asyncio.run(request())
