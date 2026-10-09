"""A job declared the AMDockVS way, run by the ms_flow engine: what its handler and finalize see."""
import pytest

from amdockvs import AMDockVSRuntime
from amdockvs.core.jobs import job, task

CALLS: list = []


class Recorder:
    def handle(self, chunk_id, result):
        CALLS.append(("handle", result))

    def on_error(self, chunk_id, error):
        CALLS.append(("on_error", "boom" in error))

    def flush(self):
        CALLS.append(("flush",))

    def on_job_terminal(self, status):
        CALLS.append(("terminal", status))

    def close(self):
        CALLS.append(("close",))


class FlushOnly:  # as the shard writers of io/shards.py: no `close`, no `on_job_terminal`
    def handle(self, chunk_id, result):
        CALLS.append(("handle", result))

    def flush(self):
        CALLS.append(("flush",))


def _finalize(params, context):
    CALLS.append(("finalize", context["terminal_status"], sorted(context)))


@task(name="double", executor="thread")
def _double(payload):
    if payload["value"] == payload["bad"]:
        raise ValueError("boom")
    return [payload["value"] * 2]


def _declare(handler):
    @job(task=_double, name="adapter_check", finalize=_finalize, result_handler_factory=handler)
    def chunks(params, config):
        assert config["project_db"] is not None
        for value in range(1, 4):
            yield {"value": value, "bad": params["bad"]}

    return chunks


@pytest.mark.parametrize("handler, bad, status", [(Recorder, 0, "completed"), (Recorder, 2, "failed"),
                                                  (FlushOnly, 0, "completed")])
def test_handler_and_finalize_follow_the_job(tmp_path, monkeypatch, handler, bad, status):
    monkeypatch.setenv("HOME", str(tmp_path))
    CALLS.clear()
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="adapter", folder=tmp_path / "adapter", description="")
        ended = runtime.wait_for_job(runtime.submit_job(_declare(handler), params={"bad": bad}))
    finally:
        runtime.shutdown()

    assert ended.status == status
    assert sorted(call[1] for call in CALLS if call[0] == "handle") == [[v * 2] for v in (1, 2, 3) if v != bad]
    assert CALLS.count(("flush",)) == 1
    assert CALLS.count(("on_error", True)) == (1 if bad else 0)
    tail = [call for call in CALLS if call[0] in ("flush", "terminal", "close", "finalize")]
    lifecycle = [("flush",), ("terminal", status), ("close",)] if handler is Recorder else [("flush",)]
    assert tail == lifecycle + [("finalize", status, ["job_params", "project_context", "project_db",
                                                       "project_resources", "project_store", "terminal_status"])]
