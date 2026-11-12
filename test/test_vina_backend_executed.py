"""The Vina backend is resolved by the host that runs a chunk, and the run records what it used."""
import sys

from ms_flow.core.database.project import ProjectStore
from sqlmodel import select

from amdockvs.core import constants
from amdockvs.docking.engines import vina
from amdockvs.docking.results.repository import (
    docking_run_id,
    record_executed,
    record_executed_from_results,
)
from amdockvs.docking.shard_jobs import ShardHitWriter
from amdockvs.models.docking import DockingResult, DockingRun
from amdockvs.models.molecules import MoleculeRecord


def _host(monkeypatch, *, binary: bool) -> None:
    """The machine a chunk runs on: with a working Vina binary, or with the bindings only."""
    monkeypatch.setattr(constants, "vina_command", lambda runtime=None: sys.executable if binary else "/nowhere/vina")
    monkeypatch.setattr(constants, "vina_backend", lambda runtime=None: "binary" if binary else "python")


def _run_chunk(monkeypatch, payload: dict) -> tuple[list[str], list[dict], list[dict]]:
    ran, told = [], []

    def fake_rows(**kwargs):
        ran.append(kwargs["vina_backend"])
        rows = [{"ligand_molecule_id": 1, "extra": {"rmsd_method": "x"}}]
        if kwargs["pair_callback"] is not None:
            kwargs["pair_callback"]([dict(row, extra=dict(row["extra"])) for row in rows])
        return rows

    monkeypatch.setattr(vina, "run_vina_docking_rows", fake_rows)
    rows = vina._vina_dock_runner({**payload, "_pair_callback": told.extend})
    return ran, rows, told


def test_a_worker_with_the_binary_uses_it_whatever_the_submitter_had(monkeypatch):
    # Submitted from a machine without the binary: the payload still asks for it.
    payload = {"vina_backend": "binary", "vina_command": "/nowhere/vina"}
    _host(monkeypatch, binary=True)
    ran, rows, told = _run_chunk(monkeypatch, payload)
    assert ran == ["binary"]
    assert rows[0]["extra"] == {"rmsd_method": "x", "vina_backend": "binary"}
    assert told[0]["extra"]["vina_backend"] == "binary"


def test_a_worker_without_the_binary_falls_back_and_says_so(monkeypatch):
    # The submitter had a binary at a path this worker does not have, nor one of its own.
    payload = {"vina_backend": "binary", "vina_command": "/submitter/only/vina"}
    _host(monkeypatch, binary=False)
    ran, rows, told = _run_chunk(monkeypatch, payload)
    assert ran == ["python"]
    assert rows[0]["extra"]["vina_backend"] == told[0]["extra"]["vina_backend"] == "python"


def _run(store, protocol=None) -> int:
    return docking_run_id(store, run_id="r", engine="vina", run_kind="screening",
                          protocol=protocol or {"hash": "h", "config": {"vina_backend": "binary"}})


def _protocol(store, run: int) -> dict:
    with store.get_session() as session:
        return dict(session.exec(select(DockingRun).where(DockingRun.id == run)).one().protocol)


def test_the_run_records_what_its_hosts_used_beside_what_it_asked_for(tmp_path):
    store = ProjectStore.open_at(tmp_path / "project.db")
    run = _run(store)
    record_executed(store, run, vina_backend=["python"])
    record_executed(store, run, vina_backend=["binary"])  # another worker had the binary
    record_executed(store, run, vina_backend=[])
    protocol = _protocol(store, run)
    assert protocol["config"]["vina_backend"] == "binary"  # the request: a re-run asks again
    assert protocol["executed"] == {"vina_backend": ["binary", "python"]}


def test_rows_written_by_the_sink_reach_the_run_at_finalize(tmp_path):
    store = ProjectStore.open_at(tmp_path / "project.db")
    run = _run(store)
    other = docking_run_id(store, run_id="r", engine="vina", run_kind="screening", protocol={"hash": "other"})
    with store.get_session() as session:
        receptor, ligand = MoleculeRecord(name="rec", is_receptor=True), MoleculeRecord(name="lig", is_ligand=True)
        session.add_all([receptor, ligand])
        session.commit()
        session.add_all([
            DockingResult(receptor_molecule_id=receptor.id, ligand_molecule_id=ligand.id, docking_run_id=run,
                          pose_rank=rank, extra={"vina_backend": "python"})
            for rank in (1, 2)
        ] + [DockingResult(receptor_molecule_id=receptor.id, ligand_molecule_id=ligand.id,
                           docking_run_id=other, extra={"vina_backend": "binary"})])
        session.commit()
    record_executed_from_results(store, run_id="r", engine="vina", protocol_hash="h")
    assert _protocol(store, run)["executed"] == {"vina_backend": ["python"]}
    assert "executed" not in _protocol(store, other)  # another protocol: not this job's


def test_the_shard_handler_records_the_backend_a_worker_reports(tmp_path):
    store = ProjectStore.open_at(tmp_path / "project.db")
    run = _run(store)
    writer = ShardHitWriter(project_db=store, hit_cap=10, hit_threshold=-5.0, run_id="r",
                            engine="vina", protocol_hash="h")
    writer.handle("", [{"shard_id": 0, "docking_run_id": run, "vina_backend": ["python"]}])
    assert _protocol(store, run)["executed"] == {"vina_backend": ["python"]}
