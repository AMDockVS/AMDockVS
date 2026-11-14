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


# --- formats with no record separator -------------------------------------------------------
# A mol/mdl block, a PDB or a PDBQT is one whole molecule with nothing to split on, so the feed
# hands the worker entries instead of a byte range and span_offset is -1. The worker used to seek
# to that offset regardless: OSError: [Errno 22] Invalid argument, with the job failing on inputs
# the ligand dialog itself offers.

ETHANOL_MOL = """ethanol
  fixture

  3  2  0  0  0  0  0  0  0  0999 V2000
    0.0000    0.0000    0.0000 C   0  0
    1.5000    0.0000    0.0000 C   0  0
    2.6000    0.8000    0.0000 O   0  0
  1  2  1  0
  2  3  1  0
M  END
"""

ETHANOL_PDB = """HETATM    1  C1  UNL     1       0.000   0.000   0.000  1.00  0.00           C
HETATM    2  C2  UNL     1       1.500   0.000   0.000  1.00  0.00           C
HETATM    3  O1  UNL     1       2.600   0.800   0.000  1.00  0.00           O
CONECT    1    2
CONECT    2    1    3
END
"""

ETHANOL_PDBQT = """REMARK  Name = ethanol
ROOT
ATOM      1  C   UNL     1       0.000   0.000   0.000  0.00  0.00    +0.000 C
ATOM      2  C   UNL     1       1.500   0.000   0.000  0.00  0.00    +0.000 C
ATOM      3  O   UNL     1       2.600   0.800   0.000  0.00  0.00    -0.400 OA
ENDROOT
TORSDOF 1
"""


def _fed_groups(tmp_path, filename, text, *, prefilter=None):
    """Drive the real feed, so what reaches the worker is the payload the feed actually emits -
    a span-less one for these formats, which is the whole point."""
    from amdockvs.io.loaders import stream_import_payload_batches

    source = tmp_path / filename
    source.write_text(text, encoding="utf-8")
    payloads = list(
        stream_import_payload_batches(
            kind="ligand",
            file_path=source,
            storage_dir=tmp_path / "shards",
            batch_size=2,
            primary_role="ligand",
            molecule_kind="small_molecule",
            prefilter=prefilter,
        )
    )
    assert payloads, "the feed emitted nothing to shard"
    assert all(int(p["span_offset"]) < 0 for p in payloads), "expected a span-less feed"
    return [group for payload in payloads for group in filter_ligand_span(payload)]


@pytest.mark.parametrize(
    "filename,text",
    [("one.mol", ETHANOL_MOL), ("one.mdl", ETHANOL_MOL), ("one.pdb", ETHANOL_PDB),
     ("one.pdbqt", ETHANOL_PDBQT)],
)
@pytest.mark.parametrize("prefilter", [None, {"max_heavy_atoms": 100}])
def test_a_separatorless_format_becomes_one_sdf_shard_record(tmp_path, filename, text, prefilter):
    groups = _fed_groups(tmp_path, filename, text, prefilter=prefilter)
    records = [record for group in groups for record in group["records"]]
    assert len(records) == 1
    assert all(group["input_format"] == "sdf" for group in groups)
    source_index, raw = records[0]
    assert source_index == 0
    mol = mol_from_record(raw, "sdf")
    assert mol is not None and mol.GetNumAtoms() == 3


def test_a_separatorless_record_is_filtered_on_its_chemistry(tmp_path):
    """Reaching the filter at all is the point: it has to be the filter that drops the record."""
    groups = _fed_groups(tmp_path, "one.mol", ETHANOL_MOL, prefilter={"max_heavy_atoms": 1})
    assert [record for group in groups for record in group["records"]] == []


def test_a_format_that_is_not_a_molecule_says_so(tmp_path):
    with pytest.raises(ValueError, match="cannot be stored as a screening-library shard"):
        _fed_groups(tmp_path, "peptides.fasta", ">one\nMKV\n")


def test_an_unreadable_molecule_fails_instead_of_importing_nothing(tmp_path):
    """This file is the whole record, so failing to read it is a failed import - not a molecule
    the filter rejected. Completing with zero shards would say the library imported fine."""
    with pytest.raises(ValueError, match="No molecule could be read"):
        _fed_groups(tmp_path, "one.pdb", "REMARK nothing here\nEND\n")
