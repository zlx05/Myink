"""工作流文本产物事件。

节点本身不依赖 Redis。Worker 在处理任务时绑定一个事件接收器；CLI、测试或其他
同步调用未绑定接收器时，这些函数自动退化为空操作。这样模型流式输出可以进入现有
SSE 通道，又不会把工作流层和具体传输设施耦合在一起。
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

ArtifactSink = Callable[[dict[str, str]], None]

_sink: ContextVar[ArtifactSink | None] = ContextVar("myink_artifact_sink", default=None)


@contextmanager
def bind_artifact_sink(sink: ArtifactSink) -> Iterator[None]:
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def emit_artifact(event: dict[str, object]) -> None:
    sink = _sink.get()
    if sink is None:
        return
    sink({key: str(value) for key, value in event.items() if value is not None})


class ArtifactEmitter:
    """把模型碎片合并成适合 Redis/SSE 的小块，并提供可重放的偏移量。"""

    def __init__(self, *, task_id: str, chapter_seq: int, stage: str,
                 attempt: int, chunk_size: int = 32):
        self.task_id = task_id
        self.chapter_seq = chapter_seq
        self.stage = stage
        self.attempt = attempt
        self.chunk_size = chunk_size
        self.artifact_id = uuid.uuid4().hex
        self._buffer = ""
        self._offset = 0

    def _base(self, event: str) -> dict[str, object]:
        return {
            "event": event,
            "task_id": self.task_id,
            "stage": self.stage,
            "chapter_seq": self.chapter_seq,
            "attempt": self.attempt,
            "artifact_id": self.artifact_id,
        }

    def start(self) -> None:
        emit_artifact({**self._base("artifact_reset"), "offset": 0})

    def reset(self) -> None:
        self._buffer = ""
        self._offset = 0
        self.artifact_id = uuid.uuid4().hex
        self.start()

    def feed(self, text: str) -> None:
        if not text:
            return
        self._buffer += text
        while len(self._buffer) >= self.chunk_size:
            self._send(self._buffer[:self.chunk_size])
            self._buffer = self._buffer[self.chunk_size:]

    def _send(self, text: str) -> None:
        emit_artifact({
            **self._base("artifact_delta"),
            "offset": self._offset,
            "content": text,
        })
        self._offset += len(text)

    def complete(self, artifact: object | None = None) -> None:
        if self._buffer:
            self._send(self._buffer)
            self._buffer = ""
        event: dict[str, object] = {
            **self._base("artifact_complete"),
            "offset": self._offset,
        }
        if artifact is not None:
            event["artifact"] = json.dumps(artifact, ensure_ascii=False)
        emit_artifact(event)

    def failed(self, reason: str) -> None:
        if self._buffer:
            self._send(self._buffer)
            self._buffer = ""
        emit_artifact({
            **self._base("artifact_failed"),
            "offset": self._offset,
            "message": reason,
        })
