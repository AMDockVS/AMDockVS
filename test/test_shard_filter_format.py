"""A shard worker has to parse a record in the format the record is *stored* in.

That is not always the format it was read in: a shard container holds SDF or SMILES, so a mol2
record is converted on the way in. Judging the converted record in its source format fails every
time, which used to discard it before the filter it was meant to be judged by ever ran — a
filtered mol2 import produced zero shards while the same file imported fine unfiltered.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("rdkit")

from amdockvs.io.shards import filter_ligand_span, mol_from_record

ETHANOL_MOL2 = """@<TRIPOS>MOLECULE
ethanol
3 2 0 0 0
SMALL
NO_CHARGES

@<TRIPOS>ATOM
1 C1 0.0 0.0 0.0 C.3 1 ETH 0.0
2 C2 1.5 0.0 0.0 C.3 1 ETH 0.0
3 O1 2.6 0.8 0.0 O.3 1 ETH 0.0
@<TRIPOS>BOND
1 1 2 1
2 2 3 1
"""


def _span(tmp_path, *, prefilter):
    mol2 = tmp_path / "one.mol2"
    mol2.write_text(ETHANOL_MOL2, encoding="utf-8")
    payload = {
        "kind": "ligand",
        "file_path": str(mol2),
        "storage_dir": str(tmp_path / "shards"),
        "input_format": "mol2",
        "primary_role": "ligand",
        "molecule_kind": "small_molecule",
        "span_offset": 0,
        "span_end": mol2.stat().st_size,
        "span_first_index": 0,
        "span_count": 1,
    }
    if prefilter is not None:
        payload["prefilter"] = {"target_molecule_kinds": ["small_molecule"], **prefilter}
    return filter_ligand_span(payload)[0]


def test_a_filter_the_molecule_passes_keeps_the_mol2_record(tmp_path):
    # Ethanol has three heavy atoms: a 100-atom bound is not a reason to drop it.
    group = _span(tmp_path, prefilter={"max_heavy_atoms": 100})
    assert len(group["records"]) == 1
    assert group["input_format"] == "sdf"  # stored as the SDF it was converted to


def test_filtering_keeps_exactly_what_an_unfiltered_import_stores(tmp_path):
    """The filter decides on chemistry, so a passing filter must not change the outcome."""
    plain = _span(tmp_path, prefilter=None)
    filtered = _span(tmp_path, prefilter={"max_heavy_atoms": 100})
    assert plain["records"] == filtered["records"]
    assert plain["input_format"] == filtered["input_format"]


def test_the_stored_record_is_readable_as_what_its_header_claims(tmp_path):
    group = _span(tmp_path, prefilter={"max_heavy_atoms": 100})
    source_index, raw = group["records"][0]
    assert source_index == 0
    mol = mol_from_record(raw, group["input_format"])
    assert mol is not None and mol.GetNumAtoms() == 3


def test_a_filter_the_molecule_fails_still_drops_the_record(tmp_path):
    assert _span(tmp_path, prefilter={"max_heavy_atoms": 1})["records"] == []
