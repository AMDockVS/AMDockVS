"""A receptor selection that names nothing in the structure is refused, not dropped.

Worth a test because each unknown selection used to fall back in silence: a chain or assembly typo
imported the whole structure, a metal or ligand typo imported none. A script got a different
receptor than it asked for and no word about it.
"""
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("gemmi")

from amdockvs.io.api import LoaderAPI
from amdockvs.io.receptor_preview import ReceptorImportOptions, build_receptor_import_maps

COMPLEX = """ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  ALA A   1       1.458   0.000   0.000  1.00  0.00           C
ATOM      3  C   ALA A   1       2.009   1.420   0.000  1.00  0.00           C
ATOM      4  O   ALA A   1       1.251   2.390   0.000  1.00  0.00           O
HETATM    5  C1  LIG A 101       4.000   4.000   4.000  1.00  0.00           C
HETATM    6  O1  LIG A 101       5.200   4.000   4.000  1.00  0.00           O
HETATM    7 ZN    ZN A 201       3.000   3.000   1.000  1.00  0.00          ZN
ATOM      8  N   ALA B   1      10.000   0.000   0.000  1.00  0.00           N
ATOM      9  CA  ALA B   1      11.458   0.000   0.000  1.00  0.00           C
ATOM     10  C   ALA B   1      12.009   1.420   0.000  1.00  0.00           C
ATOM     11  O   ALA B   1      11.251   2.390   0.000  1.00  0.00           O
END
"""

UNKNOWN = [
    ({"selected_chain_ids": ["A", "NO_SUCH_CHAIN"]}, "no chain NO_SUCH_CHAIN (has: A, B)"),
    ({"selected_assembly": "NO_SUCH_ASSEMBLY"}, "no assembly NO_SUCH_ASSEMBLY (has: none)"),
    ({"selected_metals": ["NO_SUCH_METAL"]}, "no metal NO_SUCH_METAL (has: ZN:A:201)"),
    ({"selected_reference_ligands": ["NO_SUCH_LIGAND"]}, "no reference ligand NO_SUCH_LIGAND (has: LIG:A:101)"),
    ({"selected_waters": ["HOH:A:999"]}, "no water HOH:A:999 (has: none)"),
    ({"selected_cocrystal_key": "XYZ:A:1"}, "no component XYZ:A:1"),
]

KNOWN = [
    {"selected_chain_ids": ["A", "B"]},
    {"selected_metals": ["ZN:A:201"], "selected_reference_ligands": ["LIG:A:101"]},
    {"selected_metals": [], "selected_reference_ligands": []},  # take them all off: allowed
    # What the dialog sends for a row the user never touched.
    {"selected_chain_ids": [], "selected_assembly": "", "selected_reference_ligands": None,
     "selected_metals": None, "selected_waters": None, "selected_cocrystal_key": ""},
]


@pytest.fixture
def receptor(tmp_path):
    path = tmp_path / "complex.pdb"
    path.write_text(COMPLEX, encoding="utf-8")
    return str(path)


def _maps(receptor, state):
    return build_receptor_import_maps([receptor], base_options=ReceptorImportOptions(), per_file={receptor: state})


@pytest.mark.parametrize("state,message", UNKNOWN)
def test_an_unknown_selection_is_refused_with_what_the_file_has(receptor, state, message):
    with pytest.raises(ValueError, match="Unknown receptor selection") as caught:
        _maps(receptor, state)
    assert f"complex.pdb: {message}" in str(caught.value)


@pytest.mark.parametrize("state", KNOWN)
def test_a_selection_from_the_structure_is_accepted(receptor, state):
    patches, _specs = _maps(receptor, state)
    assert receptor in patches


def test_every_problem_is_reported_at_once(receptor):
    with pytest.raises(ValueError) as caught:
        _maps(receptor, {"selected_chain_ids": ["Q"], "selected_metals": ["FE:A:1"]})
    assert "no chain Q" in str(caught.value) and "no metal FE:A:1" in str(caught.value)


def test_load_receptors_refuses_before_queuing_a_job(receptor):
    submitted = []

    class Loader(LoaderAPI):
        def _batches(self, files, **_):
            return {}

        def _temporary(self, files, temporary_files):
            return {}

        def _submit_job(self, job_def, *, params, **_):
            submitted.append(params)
            return "job"

    loader = Loader(SimpleNamespace(_require_active_project=lambda: None))
    with pytest.raises(ValueError, match="no chain NO_SUCH_CHAIN"):
        loader.load_receptors([receptor], binding_site_box_size=20.0, grid_box_metals=[],
                              per_file={receptor: {"selected_chain_ids": ["NO_SUCH_CHAIN"]}})
    assert submitted == []
    loader.load_receptors([receptor], binding_site_box_size=20.0, grid_box_metals=[],
                          per_file={receptor: {"selected_chain_ids": ["B"]}})
    assert len(submitted) == 1
