"""Chemistry reads the bytes of the row and hands back Payloads: no path in, no path out."""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from ms_flow.payloads import Payload
from rdkit import Chem

from amdockvs.chemistry import tasks as chemistry_tasks
from amdockvs.chemistry.descriptors import calculate_descriptor_rows
from amdockvs.chemistry.service import run_receptor_tool

PDB = (
    "ATOM      1  N   GLY A   1       0.000   0.000   0.000  1.00  0.00           N\n"
    "ATOM      2  CA  GLY A   1       1.400   0.000   0.000  1.00  0.00           C\n"
    "END\n"
).encode()


def test_descriptors_are_computed_from_the_row_bytes():
    block = Chem.MolToMolBlock(Chem.MolFromSmiles("CCO")).encode()
    rows = calculate_descriptor_rows([{"molecule_id": 4, "structure": block, "structure_kind": "sdf"}])
    assert rows[0]["molecule_id"] == 4 and rows[0]["status"] == "completed"
    assert rows[0]["heavy_atom_count"] == 3


def test_receptor_tool_runs_on_a_temp_file_and_returns_the_output_bytes():
    seen = {}

    def run(source: Path, output: Path):
        seen["source"], seen["output"] = source, output
        assert source.read_bytes() == PDB
        output.write_bytes(b"FIXED\n")

    assert run_receptor_tool(PDB, "pdb", run) == b"FIXED\n"
    assert not seen["source"].exists() and not seen["output"].exists()  # nothing left behind


def test_prediction_task_returns_payloads_and_keeps_the_stored_structure(monkeypatch):
    pae = io.BytesIO()
    np.save(pae, np.zeros((2, 2), dtype=np.float32))
    monkeypatch.setattr(
        chemistry_tasks, "predict_structure", lambda sequence, *, model, token=None: ({"ptm": 0.5}, b"data_x\n", pae.getvalue())
    )
    sequence_seen = {}
    monkeypatch.setattr(
        chemistry_tasks, "sequence_from_structure", lambda path: sequence_seen.setdefault("p", Path(path).read_bytes()) and "GA"
    )

    out = chemistry_tasks.receptor_prediction_task(
        {
            "row": {"id": 7, "stored_payload_id": 11},
            "sequence": "",
            "structure": PDB,
            "structure_kind": "pdb",
            "model_index": 2,
            "model": "esmfold2-fast-2026-05",
        }
    )

    molecule, = out["molecules"]
    assert sequence_seen["p"] == PDB  # the sequence was read from a temp copy of the stored bytes
    assert molecule["stored_payload_id"] == 11  # an existing canonical structure is not replaced
    assert isinstance(molecule["current_payload_id"], Payload)
    model, = out["molecule_models"]
    assert model["payload_id"] is molecule["current_payload_id"]
    assert model["pae_payload_id"].kind == "npy"


def test_pdb2pqr_receptor_row_comes_back_as_the_pdb_payload(monkeypatch):
    """The tool writes its PDB and a PQR sidecar into a work folder: the row gets the PDB bytes, and
    neither file nor the temp copy of the input outlives the call."""
    import subprocess

    from amdockvs.chemistry.service import transform_receptor_rows
    from amdockvs.chemistry.tools import receptors
    from amdockvs.core.constants import SERIES_RECEPTORS

    touched: list[Path] = []

    def fake_run(cmd, **_kwargs):
        pdb = Path(next(arg for arg in cmd if arg.startswith("--pdb-output=")).split("=", 1)[1])
        source, pqr = Path(cmd[-2]), Path(cmd[-1])
        assert source.read_bytes() == PDB  # the tool read a temp file holding the row's bytes
        pdb.write_bytes(b"PROTONATED PDB\n")
        pqr.write_bytes(b"PQR SIDECAR\n")
        touched.extend([source, pdb, pqr])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(receptors.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(receptors.subprocess, "run", fake_run)

    update, = transform_receptor_rows(
        operation="protonate",
        rows=[{"id": 9, "structure": PDB, "structure_kind": "pdb", "has_3d": True, "current_model_index": 0}],
        params={"method": "pdb2pqr"},
        next_model_index_by_entity={9: 1},
    )

    current = update["current_payload_id"]
    assert (current.data, current.kind, current.series) == (b"PROTONATED PDB\n", "pdb", SERIES_RECEPTORS)
    assert update["model_rows"][0]["payload_id"] is current and update["current_model_index"] == 1
    assert touched and not any(path.exists() for path in touched)
