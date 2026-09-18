"""worker 进程入口：`myink-worker`（pyproject scripts）。"""

from __future__ import annotations

from myink.worker.consumer import run

if __name__ == "__main__":
    run()
