"""多进程并行回归测试的 worker 子进程入口（tests/test_multiprocess.py 用 subprocess 启动）。

pytest 的 monkeypatch 不跨进程——子进程必须自注入假 provider/embedder 后再跑真实
consumer.run()。SLOW 停顿用于检测「两个 worker 是否真的同时在跑」：
  mp:active  —— setnx 占位标记（正在执行某个 generate）
  mp:overlap —— 进入时发现 mp:active 已被占（另一 worker 正在跑）则 +1

异书并行断言 overlap>0（并发确实发生过）；同书串行断言 overlap==0（永不并发，
书锁保证）。两个 key 由测试主进程在读断言前清理/读取。
"""

from __future__ import annotations

import json
import os
import sys
import time

# 子进程不保证 editable 安装，直接指到 src
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import redis  # noqa: E402

from aiink.providers.base import ModelProvider, ModelResponse  # noqa: E402
from aiink.config import settings  # noqa: E402

_R = redis.from_url(settings.redis_url, decode_responses=True)


class FakeEmbedder:
    """假 bge-m3：固定 1024 维向量，不加载真实模型（子进程复制 test_flow）。"""

    def encode(self, texts):
        return [[0.0] * 1024 for _ in texts]


class StubProvider(ModelProvider):
    """test_flow.StubProvider 的子进程复制（不依赖 pytest/monkeypatch）+ 并发检测。"""

    def __init__(self, realm_from: str, realm_to: str, slow: float = 0.0):
        self.realm_from = realm_from
        self.realm_to = realm_to
        self.slow = slow

    def name(self) -> str:
        return "stub"

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        if self.slow:
            # 并发检测：占位成功说明只有自己在跑；失败说明另一 worker 正在跑 → overlap+1
            if not _R.set("mp:active", "1", nx=True, ex=30):
                _R.incr("mp:overlap")
            time.sleep(self.slow)
            _R.delete("mp:active")

        node = self._infer_node(messages)
        if node == "cast":
            content = '{"cast":["林砚"],"locations":["黑市"]}'
        elif node == "plan":
            content = (
                '{"goals":["推进主线"],"scenes":[],"characters":[],'
                '"hooks_to_plant":[],"hooks_to_resolve":[],'
                '"expected_events":["林砚在黑市查探玉佩真相"],"hard_constraints":[]}'
            )
        elif node == "audit":
            content = '{"verdict":"pass","findings":[],"reasons":["章节符合计划"],"confidence":0.9}'
        elif node == "write":
            content = '林砚在黑市隐秘查探，玉佩气息若隐若现。夜色沉沉。'
        elif node == "extract":
            content = (
                '{"candidates":['
                '{"kind":"event","source_chapter":1,"confidence":0.9,"payload":{"summary":"林砚于黑市查探玉佩真相",'
                '"participants":["林砚"],"source_chapter":1,"confidence":0.9}},'
                f'{{"kind":"character_state","source_chapter":1,"confidence":0.9,"payload":{{"character_id":"林砚",'
                f'"field":"realm","old_value":"{self.realm_from}","new_value":"{self.realm_to}",'
                f'"source_chapter":1,"confidence":0.9}}}},'
                '{"kind":"foreshadow","source_chapter":1,"confidence":0.9,"payload":{"description":"玉佩气息异常，疑似与玉佩真相有关",'
                '"trigger":{"actor":"林砚","action":"发现","object":"玉佩"},"source_chapter":1,"confidence":0.9}}'
                ']}'
            )
        elif node == "revise":
            content = '=== CONTENT ===\n修订后正文\n=== RESPONSES ===\n[]'
        else:
            content = "{}"
        return ModelResponse(content=content, model_id=model_id, input_tokens=100,
                             output_tokens=200, duration_ms=50)

    def _infer_node(self, messages):
        sys_msg = messages[0]["content"] if messages else ""
        if "出场人物 Agent" in sys_msg:
            return "cast"
        if "规划 Agent" in sys_msg:
            return "plan"
        if "记忆抽取" in sys_msg:
            return "extract"
        if "修订 Agent" in sys_msg:
            return "revise"
        if "审核中枢" in sys_msg:
            return "audit"
        return "write"


def _main() -> None:
    import aiink.providers as providers_mod
    import aiink.memory.recall as recall_mod
    import aiink.workflow.nodes as nodes_mod

    providers_mod.default_provider = StubProvider("金丹", "金丹", slow=0.8)
    nodes_mod.get_embedder = lambda: FakeEmbedder()
    recall_mod.get_embedder = lambda: FakeEmbedder()

    from aiink.worker.consumer import run

    run()


if __name__ == "__main__":
    _main()
