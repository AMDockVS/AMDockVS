"""How AMDockVS declares a job, and what turns the declaration into a job of the ms_flow engine.

A job is a chunker `(params, config)` that yields payloads, and a `run_chunk(payload)` or
`run_chunk(payload, progress_cb)` that runs one in a worker. `config` brings the project:
`project_db`, `project_store`, `project_context` and `project_resources`. The rows a chunk
returns go to `output_spec`, or to the handler `result_handler_factory(params, config)` builds
(`handle`, `on_error`, `flush`, `on_job_terminal`, `close`): a job is its name and its params,
and what it reads of the project is read where it runs. `task_label` is what a payload goes by in the
monitor: a template its keys fill, or a function `(payload)` that returns the text.
`relative_paths` is for a job whose chunks write files the project keeps by a path relative to
its folder: a chunk names them by their whole path, which is what an executor on another disk
brings back, and they are cut to relative ones before `output_spec` writes them.

A declaration is one job of the engine under a name of its own (`engine`), so one on record
is submitted again by that name: `runtime.resume_job`. What it is given of the project is not
in it: the runtime says it in `configs`.

ponytail: the declarations stay as they were written for the engine before this one, and
`engine` is the only place that knows both. A job written straight as
`ms_flow.engine.job` goes to `runtime.workspace.submit` and needs none of this.
"""
from __future__ import annotations

import functools
import inspect
import os
import weakref
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from ms_flow.engine import job as _engine_job
from ms_flow.engine import report, writer
from ms_flow.engine.files import swap

# What a job ended as, for whoever was written against the statuses of the engine before:
# there a job with a chunk that failed had failed, and a job was never left for another run.
STATUS = {"waiting": "pending", "completed_with_errors": "failed", "interrupted": "canceled"}

# The `config` of a chunker, by the store the engine hands the job of its project: filled where
# the engine runs (`jobs_setup`), in its process when it has one.
configs: "weakref.WeakKeyDictionary[Any, dict]" = weakref.WeakKeyDictionary()


def jobs_setup(workspace) -> None:
    """`Workspace.jobs_setup`: says in `configs` what the jobs of the active project are given of it."""
    configs[workspace.project_db] = {
        "project_db": workspace.project_db,
        "project_store": workspace.advanced.project_store,
        "project_context": workspace.active_context,
        "project_resources": workspace.get_project_resource_map(),
    }


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
    """One payload in a worker. `progress_cb` takes a percentage. A `ms_flow.chain` takes `params`
    second, not a callback: its steps report for themselves."""
    return run(payload, _percent) if _takes(run) >= 2 and not hasattr(run, "steps") else run(payload)


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


class _Counted:
    """A job's chunks that know how many are left: ms_flow reads `remaining()` at the first chunk
    and every `_RECOUNT` seconds after, which gives the job its total while it runs.

    Counted once, when the engine first asks: that is after the jobs this one waits for have
    ended, so a library they build or replace is the one counted, not the one at submit time.
    ms_flow makes the total exact once the feed ends, so a count that misses is only shown, never
    waited on.
    """

    def __init__(self, definition: "JobDefinition", params: Any, project: Any):
        self._definition, self._params, self._project = definition, params, project
        self._count: Optional[int] = None
        self._given = 0

    def remaining(self) -> int:
        if self._count is None:
            try:
                self._count = self._definition.count_chunks(self._params, config=configs[self._project]) or 0
            except Exception:  # noqa: BLE001 — a total is a hint; it must never fail the feed
                self._count = 0
        return max(0, self._count - self._given)

    def __iter__(self) -> Iterator[dict]:
        for chunk in self._definition.build_chunks(self._params, config=configs[self._project]):
            self._given += 1
            yield chunk


# What of a declaration only the application reads: a copy that changes only these runs the same job.
_APP_SIDE = frozenset({"executor", "supported_executors", "on_thread"})


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
    # (params) -> its tasks only wait for a program they start: on this machine they run on threads
    # of ms_flow's helper process, and no worker of the pool is kept for them.
    on_thread: Optional[Callable[[dict], bool]] = None
    where: str = ""  # `module:qualname` of this declaration

    def with_options(self, **changes) -> "JobDefinition":
        known = {field.name for field in fields(self)}
        changes = {name: value for name, value in changes.items() if name in known}
        copy = replace(self, **changes)
        if set(changes) <= _APP_SIDE:  # the same job of the engine: it goes there by this one's name
            copy.__dict__["engine"] = self.engine
        return copy

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

    @functools.cached_property
    def engine(self):
        """This declaration as the one job of the engine `Workspace.submit` takes, named after
        it: it goes to the engine's process by that name, and `Workspace.resume` submits it again
        by it. Its handler is made there, from the params and the project (`configs`).
        ponytail: a copy (`with_options`) that changes what the engine runs answers to the name
        of the one it was copied from, so it runs only with the engine in this process and is
        not resumed. One that changes only `_APP_SIDE` is the same job."""
        run = functools.partial(_run_chunk, run=self.run_chunk)
        run.__task__ = {"cpu": max(1, int(self.cpu_required or 1)), "gpu": 0, "mem_mb": 0, "retries": 0,
                        "batch": None, "batched": False, "label": self.task_label}

        def source(params, project) -> Iterable[dict]:
            yield from self.build_chunks(params, config=configs[project])

        if self.chunk_counter is not None:
            # Lazy as well: nothing is read until the engine starts feeding, after the jobs this
            # one waits for. The source also says what it has left, which gives the job a total.
            def source(params, project) -> Iterable[dict]:  # noqa: F811
                return _Counted(self, params, project)

        output = self.output_spec
        if self.result_handler_factory is not None:
            def output(params, project):
                params = self.validate_params(params)
                return _Handled(self.result_handler_factory(params, configs[project]))
        elif self.relative_paths:
            def output(params, project):
                root = Path(configs[project]["project_context"].path).expanduser().resolve()
                return _Relative(writer(self.output_spec, project), root)

        finalize = None
        if self.finalize is not None:
            def finalize(params, project, snapshot) -> None:
                params = self.validate_params(params)
                status = STATUS.get(snapshot.status, snapshot.status)
                self.finalize(params, {"job_params": params, "terminal_status": status, **configs[project]})

        source.__name__ = self.name
        # With its model the engine keeps the params as JSON, which is what it submits again.
        made = _engine_job(task=run, name=self.name, params_model=self.params_model, output=output,
                           finalize=finalize)(source)
        if self.where:
            module, _, name = self.where.partition(":")
            made.__module__, made.__qualname__ = module, f"{name}.engine"
        made.definition = self
        return made


class _Handled:
    """The writer of a job whose rows go to its handler (`handle`, `on_error`, `flush`,
    `on_job_terminal`, `close`)."""

    def __init__(self, handler):
        self._handler, self._failed, self._closed = handler, 0, False

    def write(self, results: list) -> None:
        for result in results:
            self._handler.handle("", result)

    def on_error(self, key, error) -> None:
        self._failed += 1
        if hasattr(self._handler, "on_error"):
            self._handler.on_error(str(key), str(error))

    def finish(self) -> None:  # every chunk has ended: what it raises fails the job
        self._handler.flush()  # once, and never for a job that was stopped halfway
        self._close("failed" if self._failed else "completed")

    def ended(self, snapshot) -> None:  # a job that was stopped never got to `finish`
        self._close(STATUS.get(snapshot.status, snapshot.status))

    def _close(self, status: str) -> None:
        if self._closed:
            return
        self._closed = True
        for name, args in (("on_job_terminal", (status,)), ("close", ())):
            if hasattr(self._handler, name):
                getattr(self._handler, name)(*args)


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
        cls.definition = JobDefinition(
            where=f"{cls.__module__}:{cls.__qualname__}.definition",
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
            on_thread=cls._own("on_thread"),
        )
        return cls.definition


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
        relative_paths: bool = False, count_chunks=None, **_described):
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
            chunk_counter=count_chunks,
            task_label=task_label,
            executor=executor or getattr(task, "executor", "compute"),
            supported_executors=tuple(supported_executors) or getattr(task, "supported_executors", ()),
            cpu_required=cpu_required or getattr(task, "cpu_required", 1),
            relative_paths=relative_paths,
            where=f"{chunker.__module__}:{chunker.__qualname__}",
        )
    return declare


__all__ = ["JobDefinition", "JobSpec", "STATUS", "configs", "job", "task"]
