"""A kept cofactor reaches the PDBQT as chemistry, not as a geometry guess.

``ms_adtools.prepare_receptor`` reads a bare cofactor's types and charges off its geometry: no
bond orders, so no aromatic ring, no formal charges and a neutral phosphate. On FMN that is a
net charge of +0.001 where the answer is -2, every ring carbon typed ``C`` where ten are
aromatic, and N1/N10 swapped between donor and acceptor. ``ccd_components`` types it from the
shipped CCD component instead.

The structure here is a two-residue stand-in rather than a real PDB entry: FMN plus one glycine
to keep a polymer in the file, which is all ms_adtools needs to treat the FMN as a cofactor.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from amdockvs.docking.preparation.adtools import prepare_receptor_vina_pdbqt
from amdockvs.docking.preparation.cofactors import (
    bundled_components,
    nonstandard_resnames,
    seed_component_cache,
)

# FMN's 31 heavy atoms, from 1D3G chain A, plus a glycine so the file has a polymer residue.
_FMN_PDB = """\
ATOM      1  N   GLY A   1      44.000  36.000  12.000  1.00 10.00           N
ATOM      2  CA  GLY A   1      45.000  36.500  12.500  1.00 10.00           C
ATOM      3  C   GLY A   1      45.500  37.500  11.500  1.00 10.00           C
ATOM      4  O   GLY A   1      46.000  38.500  11.800  1.00 10.00           O
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


def _fmn_atoms(pdbqt: Path) -> list[str]:
    return [line for line in pdbqt.read_text().splitlines()
            if line.startswith(("ATOM", "HETATM")) and line[17:20].strip() == "FMN"]


def _net_charge(rows: list[str]) -> float:
    return sum(float(line[70:76]) for line in rows)


def _types(rows: list[str]) -> dict[str, str]:
    return {line[12:16].strip(): line[77:79].strip() for line in rows}


@pytest.fixture
def receptor(tmp_path):
    source = tmp_path / "fmn.pdb"
    source.write_text(_FMN_PDB)
    return source


def _prepare(source: Path, tmp_path: Path, *, ccd: bool):
    pytest.importorskip("ms_adtools")
    out = tmp_path / f"rec_ccd{int(ccd)}.pdbqt"
    result = prepare_receptor_vina_pdbqt(source_path=source, output_path=out, ccd_components=ccd)
    rows = _fmn_atoms(Path(result.artifact.path))
    if not rows:
        pytest.skip("ms_adtools dropped the cofactor; nothing to assert about its typing")
    return result, rows


def test_the_verified_components_are_shipped_so_the_common_path_needs_no_network():
    # The whole offline story rests on these files being there; a rename would make the CCD
    # pass fetch instead, which a test run without network would not notice any other way.
    # Each of these has a prepared structure behind it, at a whole-number charge matching the
    # chemistry: FMN/FAD 1D3G,3I65,3GRS; NAD 1HDX; ATP 1ATP; TPP 1TRK; HEM 1A6M; NAP 4LBS;
    # COA 4EA7; ADP 13CI; AMP 2CNQ; PQQ 1W6S. Nothing goes in here without one.
    shipped = set(bundled_components())
    assert {"FMN", "FAD", "NAD", "ATP", "TPP", "HEM",
            "NAP", "COA", "ADP", "AMP", "PQQ"} <= shipped
    # PLP and SAM/SAH are deliberately absent -- see test_a_missing_atom_is_reported and the
    # branch notes: PLP entries routinely omit O4A, and the amino-acid-derived pair come out a
    # whole charge too negative because acids ionize but bases are never protonated.
    assert not ({"PLP", "SAM", "SAH"} & shipped)


def test_a_missing_atom_is_reported_not_swallowed():
    """A component is typed with whole-number formal charges, so a copy must land on an integer.

    PLP is why this exists: PDB entries routinely leave out its O4A, the template match then
    goes partial, and the copy comes out at -1.70 where the answer is -2 -- an error a third of
    a charge wide that nothing in the file itself reveals.
    """
    from amdockvs.docking.preparation.cofactors import fractional_charges

    def row(resname, serial, charge):  # a PDBQT line with the charge where the parser reads it
        line = [" "] * 79
        line[0:6] = list("HETATM")
        line[12:16] = list(f"{'C':<4}")
        line[17:20] = list(f"{resname:>3}")
        line[21] = "A"
        line[22:26] = list(f"{serial:>4}")
        line[70:76] = list(f"{charge:>6.3f}")
        line[77:79] = list(" C")
        return "".join(line)

    whole = "\n".join([row("NAD", 1, -0.5), row("NAD", 1, -0.5)])          # -1.000
    assert fractional_charges(whole, ["NAD"]) == []

    broken = "\n".join([row("PLP", 2, -0.851), row("PLP", 2, -0.850)])     # -1.701
    [(resname, where, net)] = fractional_charges(broken, ["PLP"])
    assert resname == "PLP" and where == "A2"
    assert net == pytest.approx(-1.701, abs=0.001)

    # a residue nobody asked about is not the preparation's business
    assert fractional_charges(broken, ["NAD"]) == []


def test_a_components_own_metal_is_charged(tmp_path, monkeypatch):
    """Heme is the case this exists for.

    The component leaves its iron's charge unset and puts -1 on two of the four porphyrin
    nitrogens; the typing path keeps metals out of the Gasteiger pass and hands the metal back
    what its bonded atoms lost, so the iron is +2 and the heme is -2. Without
    ccd_metal_charges the iron stays at 0 and the heme reaches the grid as a -4 ion, which is
    the wrong sign for the whole site.
    """
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOLSUITE_OFFLINE", "1")  # the shipped HEM must carry this alone
    pytest.importorskip("ms_adtools")
    source = Path(__file__).resolve().parent / "data" / "hem_fixture.pdb"
    out = tmp_path / "hem.pdbqt"
    result = prepare_receptor_vina_pdbqt(source_path=source, output_path=out,
                                         ccd_components=True)
    rows = [line for line in Path(result.artifact.path).read_text().splitlines()
            if line.startswith(("ATOM", "HETATM")) and line[17:20].strip() == "HEM"]
    if not rows:
        pytest.skip("ms_adtools dropped the heme; nothing to assert about its charges")
    iron = [line for line in rows if line[12:16].strip().upper() == "FE"]
    assert iron, "the heme kept its iron"
    assert sum(float(line[70:76]) for line in iron) == pytest.approx(2.0, abs=0.05)
    assert sum(float(line[70:76]) for line in rows) == pytest.approx(-2.0, abs=0.15)
    assert not result.warnings


def test_nonstandard_resnames_finds_the_cofactor_and_not_the_polymer():
    found = nonstandard_resnames(_FMN_PDB)
    assert found == ["FMN"]


def test_seeding_is_idempotent_and_leaves_the_component_in_the_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "home"))
    pytest.importorskip("ms_adtools")
    from ms_adtools.ccd import cache_dir

    assert "FMN" in seed_component_cache()
    assert (cache_dir() / "FMN.cif").is_file()
    assert seed_component_cache() == []  # already there: not copied twice


def test_the_geometry_guess_is_what_we_are_fixing(receptor, tmp_path, monkeypatch):
    """Without the CCD pass FMN is flat-wrong, so the next test is measuring something."""
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOLSUITE_OFFLINE", "1")
    _, rows = _prepare(receptor, tmp_path, ccd=False)
    assert _net_charge(rows) == pytest.approx(0.0, abs=0.1)
    assert "A" not in set(_types(rows).values())  # no aromatic ring perceived


def test_the_ccd_component_charges_and_aromatizes_the_cofactor(receptor, tmp_path, monkeypatch):
    # Offline on purpose: the shipped component must carry this on its own.
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOLSUITE_OFFLINE", "1")
    result, rows = _prepare(receptor, tmp_path, ccd=True)

    assert _net_charge(rows) == pytest.approx(-2.0, abs=0.1), "FMN is a dianion at pH 7"
    types = _types(rows)
    assert sum(1 for t in types.values() if t == "A") >= 8, "isoalloxazine ring is aromatic"
    # Oxidized flavin: N1 accepts, N10 is the tertiary bridge to the ribityl and does not.
    assert types["N1"] == "NA"
    assert types["N10"] == "N"
    assert result.metadata.get("ccd_components") is True
    assert not result.warnings


def test_a_cofactor_with_no_component_is_reported_not_swallowed(tmp_path, monkeypatch):
    """An unshipped cofactor offline keeps the guessed charges — the run must say so."""
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOLSUITE_OFFLINE", "1")
    source = tmp_path / "zzz.pdb"
    # ZZZ is not a CCD code and is not shipped, so neither route can resolve it.
    source.write_text(_FMN_PDB.replace("FMN", "ZZZ"))
    pytest.importorskip("ms_adtools")
    out = tmp_path / "rec.pdbqt"
    result = prepare_receptor_vina_pdbqt(source_path=source, output_path=out, ccd_components=True)
    assert any("ZZZ" in warning for warning in result.warnings)


def test_a_warning_survives_the_row_pipeline(tmp_path, monkeypatch, receptor):
    """The warnings have to reach the caller, or none of the rest of this file matters.

    They were computed and dropped once already: `_prepare_row` returned (prepared, flex) and
    `result.warnings` went nowhere, so every warning this module raises was invisible in the
    app while every test here still passed. This is the test that would have caught it.
    """
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOLSUITE_OFFLINE", "1")
    pytest.importorskip("ms_adtools")
    from amdockvs.docking.preparation.entities import prepare_entities_rows

    # ZZZ resolves to no component at all, so the preparation has something to report.
    source = tmp_path / "zzz.pdb"
    source.write_text(_FMN_PDB.replace("FMN", "ZZZ"))
    rows = [{"id": 1, "structure": source.read_bytes(), "structure_kind": "pdb"}]
    summary = prepare_entities_rows(entity_kind="receptor", engine="ad4", rows=rows,
                                    keep_cofactors=True)

    assert summary["success_count"] == 1, summary.get("failures")
    assert summary["warnings"], "a warning was raised and the summary dropped it"
    assert summary["warnings"][0]["entity_id"] == 1
    assert "ZZZ" in summary["warnings"][0]["message"]
