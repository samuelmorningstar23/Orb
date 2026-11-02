"""Provider URL seam: task-owned leases in pool mode; loopback in legacy mode."""

from __future__ import annotations

from offline_policy import local_model_origin


def inference_url(path: str, *, model: str | None, base_url: str | None = None) -> str:
    from services.llm_queue_service import llm_queue

    if llm_queue.worker_pool is not None:
        return llm_queue.route_url(path, model=model)
    if base_url is None:
        import config

        base_url = config.OLLAMA_BASE_URL
    return local_model_origin(base_url) + path


def record_worker_status(status: int) -> None:
    from services.llm_queue_service import llm_queue

    worker = llm_queue.current_worker()
    if worker is not None:
        if status >= 500:
            worker.record_failure()
        elif status < 400:
            worker.record_success()


def record_worker_transport_failure() -> None:
    from services.llm_queue_service import llm_queue

    worker = llm_queue.current_worker()
    if worker is not None:
        worker.record_failure()
