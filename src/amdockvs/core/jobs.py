"""How AMDockVS declares a job, and what turns the declaration into a job of the ms_flow engine.

A job is a chunker `(params, config)` that yields payloads, and a `run_chunk(payload)` or
`run_chunk(payload, progress_cb)` that runs one in a worker. `config` brings the project:
`project_db`, `project_store`, `project_context` and `project_resources`. The rows a chunk
returns go to `output_spec`, or to the handler `result_handler_factory` builds (`handle`,
`on_error`, `flush`, `on_job_terminal`, `close`). `task_label` is what a payload goes by in the
monitor: a template its keys fill, or a function `(payload)` that returns the text.
`relative_paths` is for a job whose chunks write files the project keeps by a path relative to
its folder: a chunk names them by their whole path, which is what an executor on another disk
brings back, and they are cut to relative ones before `output_spec` writes them.

ponytail: the declarations stay as they were written for the engine before this one, and
`engine_job` is the only place that knows both. A job written straight as
`ms_flow.engine.job` goes to `runtime.workspace.submit` and needs none of this.
"""
from __future__ import annotations

import functools
import inspect
import os
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from ms_flow.engine import job as _engine_job
from ms_flow.engine import report, writer
from ms_flow.engine.files import swap

# What a job ended as, for whoever was written against the statuses of the engine before:
# there a job with a chunk that failed had failed, and a job was never left for another run.
STATUS = {"waiting": "pending", "completed_with_errors": "failed", "interrupted": "canceled"}


def _takes(fn: Callable[..., Any]) -> int:
    try:
        return len(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return 1


def _with_config(fn: Callable[..., Any], params: dict, config: dict) -> Any:
    return fn(params, config) if _takes(fn) >= 2 else fn(params)


def _percent(value: float) -> None:
    report(float(value) / 100.0)


def _run_chunk(payload: dict, run: Callable[..., Any]) -> Any:
    """One payload in a worker. `progress_cb` takes a percentage."""
    return run(payload, _percent) if _takes(run) >= 2 else run(payload)


class _Relative:
    """The writer of a sink, for results that name files of the project by their whole path."""

    def __init__(self, sink, root: Path):
        self._sink, self._root = sink, str(root) + os.sep

    def write(self, results: list) -> None:
        def cut(text: str) -> str:
            return text[len(self._root):] if text.startswith(self._root) else text

        self._sink.write([swap(result, cut, str) for result in results])

    def __getattr__(self, name: str):  # `tables`, and `finish` when the sink has one
        return getattr(self._sink, name)


@dataclass(frozen=True)
class JobDefinition:
    name: str
    run_chunk: Callable[..., Any]
    chunker: Callable[..., Any]
    params_model: Any = None
    output_spec: Any = None
    finalize: Optional[Callable[..., Any]] = None
    result_handler_factory: Optional[Callable[..., Any]] = None
    chunk_counter: Optional[Callable[..., Any]] = None
    task_label: Any = None
    executor: str = "compute"
    supported_executors: tuple[str, ...] = ()
    cpu_required: int = 1
    relative_paths: bool = False

    def with_options(self, **changes) -> "JobDefinition":
        known = {field.name for field in fields(self)}
        return replace(self, **{name: value for name, value in changes.items() if name in known})

    def validate_params(self, params: Any) -> dict:
        if self.params_model is None:
            return params.model_dump(mode="python") if hasattr(params, "model_dump") else dict(params or {})
        return self.params_model.model_validate(params).model_dump(mode="python")

    def build_chunks(self, params: Any, *, config: Optional[dict] = None) -> Iterator[dict]:
        produced = _with_config(self.chunker, self.validate_params(params), dict(config or {}))
        return iter([produced] if isinstance(produced, dict) else produced)

    def count_chunks(self, params: Any, *, config: Optional[dict] = None) -> Optional[int]:
        if self.chunk_counter is None:
            return None
        return max(0, int(_with_config(self.chunk_counter, self.validate_params(params), dict(config or {}))))

    def submit_with_options(self, runtime, *, params: Any, job_options: Optional[dict] = None, **kwargs) -> str:
        return runtime.submit_job(self.with_options(**job_options) if job_options else self, params=params, **kwargs)

    def engine_job(self, *, config: dict, cpu: Optional[int] = None,
                   handler_kwargs: Optional[dict] = None):
        """This job as `Workspace.submit` takes it, and the `on_error` to submit it with.

        The chunker starts when the engine reads it, which is once the jobs it waits for ended.
        ponytail: built for each submit, so the engine cannot name it and `Workspace.resume`
        does not know it. A module-level `ms_flow.engine.job` when a job has to be resumed.
        """
        run = functools.partial(_run_chunk, run=self.run_chunk)
        run.__task__ = {"cpu": max(1, int(cpu or self.cpu_required or 1)), "gpu": 0, "mem_mb": 0, "retries": 0,
                        "batch": None, "batched": False, "label": self.task_label}
        handler = (self.result_handler_factory(**dict(handler_kwargs or {}))
                   if self.result_handler_factory is not None else None)
        state = {"failed": 0, "closed": False}

        def close(status: str) -> None:
            if handler is None or state["closed"]:
                return
            state["closed"] = True
            for name, args in (("on_job_terminal", (status,)), ("close", ())):
                if hasattr(handler, name):
                    getattr(handler, name)(*args)

        class Writer:
            @staticmethod
            def write(results: list) -> None:
                for result in results:
                    handler.handle("", result)

            @staticmethod
            def finish() -> None:  # every chunk has ended: what it raises fails the job
                handler.flush()  # once, and never for a job that was stopped halfway
                close("failed" if state["failed"] else "completed")

        def on_error(key, error) -> None:
            state["failed"] += 1
            if handler is not None and hasattr(handler, "on_error"):
                handler.on_error(str(key), str(error))

        def ended(params: dict, project, snapshot) -> None:
            status = STATUS.get(snapshot.status, snapshot.status)
            try:
                close(status)  # a job that was stopped never got to `finish`
            finally:
                if self.finalize is not None:
                    self.finalize(params, {"job_params": params, "terminal_status": status, **config})

        def source(params: dict, project) -> Iterable[dict]:
            yield from self.build_chunks(params, config=config)

        output = (lambda params, project: Writer) if handler is not None else self.output_spec
        if self.relative_paths and handler is None:
            root = Path(config["project_context"].path).expanduser().resolve()

            def output(params, project):
                return _Relative(writer(self.output_spec, project), root)
        source.__name__ = self.name
        return _engine_job(task=run, name=self.name, output=output, finalize=ended)(source), on_error


class JobSpec:
    """A job declared as a class: `build_chunks`, `run_chunk` and, when it has them, `count_chunks`,
    `finalize` and `result_handler_factory`."""

    name: str = ""
    params_model: Any = None
    executor: str = "compute"
    supported_executors: tuple[str, ...] = ()
    cpu_required: int | None = 1
    output_spec: Any = None
    task_label: Any = None

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterable[dict]:
        yield params

    @staticmethod
    def run_chunk(payload: dict, progress=None) -> Any:
        raise NotImplementedError("JobSpec subclasses must implement run_chunk().")

    @classmethod
    def _own(cls, name: str) -> Callable[..., Any] | None:
        value = getattr(cls, name) if name in cls.__dict__ else None
        return value if callable(value) else None

    @classmethod
    def to_job_definition(cls) -> JobDefinition:
        return JobDefinition(
            name=str(cls.name or cls.__name__),
            run_chunk=cls.run_chunk,
            chunker=cls.build_chunks,
            params_model=cls.params_model,
            output_spec=cls.output_spec,
            finalize=cls._own("finalize"),
            result_handler_factory=cls._own("result_handler_factory"),
            chunk_counter=cls._own("count_chunks"),
            task_label=cls.task_label,
            executor=str(cls.executor or "compute"),
            supported_executors=tuple(cls.supported_executors or ()),
            cpu_required=cls.cpu_required or 1,
        )


def task(*, name: Optional[str] = None, executor: str = "compute", supported_executors: Iterable[str] = (),
         cpu_required: int = 1, **_described):
    """Mark `run_chunk`. It stays the plain function it is."""
    def mark(fn):
        fn.task_name, fn.executor = name or fn.__name__, executor
        fn.supported_executors, fn.cpu_required = tuple(supported_executors), cpu_required
        return fn
    return mark


def job(*, task, name: Optional[str] = None, params_model: Any = None, executor: str = "",
        supported_executors: Iterable[str] = (), cpu_required: Optional[int] = None, finalize=None,
        output_spec: Any = None, result_handler_factory=None, task_label: Any = None,
        relative_paths: bool = False, **_described):
    """Declare the job a chunker feeds: its payloads go to `task`."""
    def declare(chunker) -> JobDefinition:
        return JobDefinition(
            name=name or chunker.__name__,
            run_chunk=task,
            chunker=chunker,
            params_model=params_model,
            output_spec=output_spec,
            finalize=finalize,
            result_handler_factory=result_handler_factory,
            task_label=task_label,
            executor=executor or getattr(task, "executor", "compute"),
            supported_executors=tuple(supported_executors) or getattr(task, "supported_executors", ()),
            cpu_required=cpu_required or getattr(task, "cpu_required", 1),
            relative_paths=relative_paths,
        )
    return declare


__all__ = ["JobDefinition", "JobSpec", "STATUS", "job", "task"]
