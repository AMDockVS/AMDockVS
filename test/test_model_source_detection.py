"""An imported receptor says whether it is a prediction: by its header, else by its B-factors."""
from __future__ import annotations

import gemmi
import pytest
from sqlmodel import select

from amdockvs.core.vocab import ModelSource
from amdockvs.io.receptor_preview import model_source_of


def _pdb(b_factors_per_residue, *, header: str = "") -> str:
    """Two atoms per alanine; each residue's pair of B-factors as given."""
    lines = [header] if header else []
    serial = 0
    for number, (first, second) in enumerate(b_factors_per_residue, start=1):
        for name, b_factor, x in (("N", first, 0.0), ("CA", second, 1.46)):
            serial += 1
            lines.append(f"ATOM  {serial:5d}  {name:<3} ALA A{number:4d}    {x + number * 3.8:8.3f}"
                         f"{0:8.3f}{0:8.3f}  1.00{b_factor:6.2f}           {name[0]}")
    return "\n".join([*lines, "END", ""])


PLDDT = [(91.2, 91.2), (85.0, 85.0), (40.3, 40.3), (77.7, 77.7)]
EXPERIMENTAL = [(12.3, 14.1), (20.0, 18.4), (33.1, 30.0), (15.5, 16.0)]


@pytest.mark.parametrize("text, header, expected", [
    (_pdb(EXPERIMENTAL), "", ModelSource.IMPORTED),
    (_pdb(PLDDT), "", ModelSource.UNKNOWN_MODEL),  # one value per residue, 0-100
    (_pdb([(0.91, 0.88), (0.42, 0.40)]), "", ModelSource.UNKNOWN_MODEL),  # ESMFold's 0-1 scale
    (_pdb([(0.0, 0.0), (0.0, 0.0)]), "", ModelSource.IMPORTED),  # OpenMM's zeros: P2Rank's case
    # A low-resolution refinement writes one B per residue too; its method settles it.
    (_pdb(PLDDT, header="EXPDTA    X-RAY DIFFRACTION"), "", ModelSource.IMPORTED),
    (_pdb(PLDDT), "data_AF-P69905-F1\n#\n", ModelSource.ALPHAFOLD_DB),
    (_pdb(PLDDT), "TITLE     ALPHAFOLD MONOMER V2.0 PREDICTION FOR HEMOGLOBIN\n", ModelSource.ALPHAFOLD_DB),
    (_pdb(PLDDT), "TITLE     ESMFOLD V0 PREDICTION FOR MGYP000542242899\n", ModelSource.ESM_ATLAS),
])
def test_model_source_from_header_then_b_factors(text, header, expected):
    assert model_source_of(gemmi.read_pdb_string(text), header.encode()) == expected


def test_an_imported_prediction_is_stored_as_one(tmp_path):
    """From the file to the molecule_models row: the code is one the project db accepts."""
    from amdockvs.io.jobs import IMPORT_GRAPH_OUTPUT
    from amdockvs.io.payloads import ImportBatchPayload
    from amdockvs.io.transformers.materializers import build_import_graph_payload
    from amdockvs.io.transformers.structures import _materialize_structure_rows
    from amdockvs.models import MoleculeModel
    from ms_flow.core.database import ProjectStore

    source = tmp_path / "AF-P69905-F1-model_v6.pdb"
    source.write_text(_pdb(PLDDT, header="TITLE     ALPHAFOLD MONOMER V2.0 PREDICTION FOR X"), encoding="utf-8")
    batch = ImportBatchPayload(kind="receptor", file_path=source, storage_dir=tmp_path, input_format="pdb",
                               primary_role="receptor", molecule_kind="protein")
    rows = list(_materialize_structure_rows(batch=batch, entries=[]))
    store = ProjectStore.open_at(tmp_path / "project.db")
    try:
        store.persist_output_spec(IMPORT_GRAPH_OUTPUT, build_import_graph_payload(rows))
        with store.get_session() as session:
            assert [model.source for model in session.exec(select(MoleculeModel))] == [ModelSource.ALPHAFOLD_DB]
    finally:
        ProjectStore.clear_cached_stores()
