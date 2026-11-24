"""Kept cofactors are typed by ms_adtools, and what it warns about reaches `receptor_preps`.

FMN plus one glycine (a polymer, so FMN is a cofactor to ms_adtools) and a bare Mg.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.docking.preparation.adtools import prepare_receptor_vina_pdbqt

_FMN_PDB = """\
ATOM      1  N   GLY A   1      44.000  36.000  12.000  1.00 10.00           N
ATOM      2  CA  GLY A   1      45.000  36.500  12.500  1.00 10.00           C
ATOM      3  C   GLY A   1      45.500  37.500  11.500  1.00 10.00           C
ATOM      4  O   GLY A   1      46.000  38.500  11.800  1.00 10.00           O
HETATM    4 MG    MG A 500      50.000  40.000  15.000  1.00 10.00          MG
HETATM    5  N1  FMN A 398      41.930  36.336   8.417  1.00 13.35           N
HETATM    6  C2  FMN A 398      42.343  35.713   9.541  1.00 13.89           C
HETATM    7  O2  FMN A 398      41.808  35.813  10.613  1.00 14.54           O
HETATM    8  N3  FMN A 398      43.488  34.881   9.456  1.00 13.23           N
HETATM    9  C4  FMN A 398      44.221  34.676   8.306  1.00 13.09           C
HETATM   10  O4  FMN A 398      45.233  33.906   8.323  1.00 13.60           O
HETATM   11  C4A FMN A 398      43.779  35.364   7.113  1.00 12.70           C
HETATM   12  N5  FMN A 398      44.433  35.228   5.969  1.00 12.52           N
HETATM   13  C5A FMN A 398      44.029  35.890   4.823  1.00 12.26           C
HETATM   14  C6  FMN A 398      44.729  35.754   3.619  1.00 12.52           C
HETATM   15  C7  FMN A 398      44.341  36.406   2.470  1.00 12.70           C
HETATM   16  C7M FMN A 398      45.093  36.264   1.176  1.00 13.09           C
HETATM   17  C8  FMN A 398      43.223  37.237   2.504  1.00 12.52           C
HETATM   18  C8M FMN A 398      42.794  37.949   1.253  1.00 13.23           C
HETATM   19  C9  FMN A 398      42.512  37.389   3.681  1.00 12.26           C
HETATM   20  C9A FMN A 398      42.900  36.734   4.846  1.00 12.00           C
HETATM   21  N10 FMN A 398      42.207  36.866   6.024  1.00 12.26           N
HETATM   22  C10 FMN A 398      42.633  36.199   7.161  1.00 12.52           C
HETATM   23  C1' FMN A 398      41.000  37.700   6.080  1.00 12.70           C
HETATM   24  C2' FMN A 398      39.800  36.900   6.580  1.00 13.09           C
HETATM   25  O2' FMN A 398      39.500  35.900   5.620  1.00 13.60           O
HETATM   26  C3' FMN A 398      38.600  37.800   6.850  1.00 13.23           C
HETATM   27  O3' FMN A 398      38.900  38.800   7.810  1.00 13.89           O
HETATM   28  C4' FMN A 398      37.400  37.000   7.350  1.00 13.35           C
HETATM   29  O4' FMN A 398      37.100  36.000   6.390  1.00 14.54           O
HETATM   30  C5' FMN A 398      36.200  37.900   7.620  1.00 13.60           C
HETATM   31  O5' FMN A 398      35.900  38.900   8.580  1.00 14.00           O
HETATM   32  P   FMN A 398      34.600  39.800   8.900  1.00 14.50           P
HETATM   33  O1P FMN A 398      34.300  40.700   7.740  1.00 15.00           O
HETATM   34  O2P FMN A 398      33.500  38.800   9.200  1.00 15.00           O
HETATM   35  O3P FMN A 398      34.800  40.700  10.100  1.00 15.00           O
END
"""


def _charges(path, resname):
    return [float(l[70:76]) for l in Path(path).read_text().splitlines()
            if l.startswith(("ATOM", "HETATM")) and l[17:20].strip() == resname]


def test_cofactor_typed_and_ions_charged_only_when_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("MOLSUITE_OFFLINE", "1")
    source = tmp_path / "fmn.pdb"
    source.write_text(_FMN_PDB)
    plain = prepare_receptor_vina_pdbqt(source_path=source, output_path=tmp_path / "plain.pdbqt")
    typed = prepare_receptor_vina_pdbqt(source_path=source, output_path=tmp_path / "typed.pdbqt",
                                        ccd_components=True, ion_charges=True)
    assert abs(sum(_charges(typed.artifact.path, "FMN")) + 2) < 0.01  # phosphate ionized at pH 7
    assert abs(sum(_charges(plain.artifact.path, "FMN")) + 2) > 0.5  # the geometry guess
    assert _charges(plain.artifact.path, "MG") == [0.0] and _charges(typed.artifact.path, "MG") == [2.0]
    assert typed.warnings == ()


def test_a_warning_reaches_the_receptor_prep_row(tmp_path, monkeypatch):
    """Through the job sink into `receptor_preps`; a clean re-preparation clears it."""
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOLSUITE_OFFLINE", "1")
    from sqlmodel import select

    from amdockvs.docking.preparation.jobs import PREP_UPSERT
    from amdockvs.docking.preparation.tasks import prepare_entities
    from amdockvs.models import MoleculeRecord, ReceptorPrep
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name="prep-warnings", folder=tmp_path / "project")
    db = runtime.workspace.project_db
    with db.get_session() as session:
        record = MoleculeRecord(name="R", molecule_type="protein", is_receptor=True)
        session.add(record)
        session.commit()
        receptor_id = int(record.id)

    def prepare(text):
        rows = [{"id": receptor_id, "structure": text.encode(), "structure_kind": "pdb"}]
        db.persist_output_spec(PREP_UPSERT, prepare_entities(
            {"entity_kind": "receptor", "engine": "ad4", "rows": rows, "keep_cofactors": True}))
        with db.get_session() as session:
            [row] = session.exec(select(ReceptorPrep)).all()
            return row.rigid_payload_id, row.error, row.warnings

    # The CCD pass runs for a receptor holding a kept cofactor (the FMN), and types its other
    # non-standard residues too: here one with no component anywhere.
    unknown = "".join(line.replace("FMN A 398", "ZZZ A 399") + "\n" for line in _FMN_PDB.splitlines()
                      if line.startswith("HETATM") and "FMN" in line)
    payload, error, warnings = prepare(_FMN_PDB.replace("END", unknown + "END") if "END" in _FMN_PDB
                                       else _FMN_PDB + unknown)
    assert payload is not None and error is None, "a warning must not mark the receptor failed"
    assert warnings and "ZZZ" in warnings
    payload, error, warnings = prepare(_FMN_PDB)
    assert payload is not None and error is None and warnings is None
