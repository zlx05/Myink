"""Plan/正文流式传输的纯单元回归，不连接外部模型或基础设施。"""

from __future__ import annotations

from types import SimpleNamespace

from myink.providers.deepseek import DeepSeekProvider
from myink.workflow.streaming import ArtifactEmitter, bind_artifact_sink


def test_artifact_emitter_chunks_with_replay_offsets():
    events: list[dict[str, str]] = []
    with bind_artifact_sink(events.append):
        emitter = ArtifactEmitter(
            task_id="task-1", chapter_seq=17, stage="write", attempt=2, chunk_size=3,
        )
        emitter.start()
        emitter.feed("abcdefg")
        emitter.complete()

    assert [event["event"] for event in events] == [
        "artifact_reset", "artifact_delta", "artifact_delta", "artifact_delta", "artifact_complete",
    ]
    deltas = [event for event in events if event["event"] == "artifact_delta"]
    assert [(event["offset"], event["content"]) for event in deltas] == [
        ("0", "abc"), ("3", "def"), ("6", "g"),
    ]
    assert events[-1]["offset"] == "7"
    assert len({event["artifact_id"] for event in events}) == 1


def test_deepseek_stream_aggregates_text_usage_and_tool_calls():
    provider = DeepSeekProvider(api_key="test-key", base_url="https://invalid.local")
    chunks = [
        SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content="甲", reasoning_content=None, tool_calls=None,
            ))],
        ),
        SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content="乙", reasoning_content=None,
                tool_calls=[SimpleNamespace(
                    index=0, id="call-1",
                    function=SimpleNamespace(name="lookup", arguments='{"id":'),
                )],
            ))],
        ),
        SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content=None, reasoning_content=None,
                tool_calls=[SimpleNamespace(
                    index=0, id=None,
                    function=SimpleNamespace(name=None, arguments='1}'),
                )],
            ))],
        ),
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=12, completion_tokens=4, prompt_cache_hit_tokens=3,
            ),
            choices=[],
        ),
    ]
    calls: list[dict] = []
    deltas: list[str] = []
    observed_before_stream_end: list[list[str]] = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            def stream():
                yield chunks[0]
                # 生成器被请求下一块之前，Provider 必须已经把上一块交给 SSE 回调。
                observed_before_stream_end.append(list(deltas))
                yield from chunks[1:]
            return stream()

    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    response = provider.generate_stream(
        [{"role": "user", "content": "写"}], model_id="deepseek-v4-flash",
        on_delta=deltas.append, tools=[{"type": "function"}],
    )

    assert deltas == ["甲", "乙"]
    assert observed_before_stream_end == [["甲"]]
    assert response.content == "甲乙"
    assert response.input_tokens == 12
    assert response.output_tokens == 4
    assert response.cache_hit is True
    assert response.tool_calls == [{"id": "call-1", "name": "lookup", "arguments": {"id": 1}}]
    assert calls[0]["stream"] is True
    assert calls[0]["stream_options"] == {"include_usage": True}


def test_stream_strips_leading_think_and_never_emits_reasoning():
    """对齐 inkos：起始 think 块与 reasoning_details 都不经 on_delta 进正文。"""
    provider = DeepSeekProvider(api_key="test-key", base_url="https://invalid.local")
    chunks = [
        SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content="<th", reasoning_content=None, reasoning_details=None, tool_calls=None,
            ))],
        ),
        SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content="ink>先推敲剧情</think>\n他推开门。",
                reasoning_content=None, reasoning_details=None, tool_calls=None,
            ))],
        ),
        SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content=None, reasoning_content=None,
                reasoning_details=[{"text": "这段不该出现在正文"}],
                tool_calls=None,
            ))],
        ),
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=4, completion_tokens=2, prompt_cache_hit_tokens=0,
            ),
            choices=[],
        ),
    ]

    class FakeCompletions:
        def create(self, **kwargs):
            def stream():
                yield from chunks
            return stream()

    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    deltas: list[str] = []
    response = provider.generate_stream(
        [{"role": "user", "content": "写"}], model_id="novel-pro",
        on_delta=deltas.append, disable_thinking=True,
    )
    assert "".join(deltas) == "他推开门。"
    assert "推敲" not in "".join(deltas)
    assert "不该出现" not in "".join(deltas)
    assert response.content == "他推开门。"
    assert response.error is None
