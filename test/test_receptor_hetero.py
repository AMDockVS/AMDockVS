"""Waters, cofactors and metals from import through to the receptor PDBQT.

Import keeps what its options say to keep; the preparation step decides what of that actually
reaches the receptor PDBQT. Both halves used to be overridden by a blanket polymer+metal whitelist.
"""
from amdockvs.docking.preparation.entities import receptor_excluded_resnames
from amdockvs.docking.preparation.adtools import _without_resnames

_BLOCK = "\n".join(
    [
        "ATOM      1  N   ASP A 285      -3.256  -3.631  -1.685  1.00  0.00           N",
        "HETATM    2 ZN    ZN A 900     -37.000 -35.000 -12.000  1.00 20.00          ZN",
        "HETATM    3  O   HOH A2001     -31.464 -37.112 -11.121  1.00 34.40           O",
        "HETATM    4  FE  HEM A 500     -30.000 -30.000 -10.000  1.00 20.00          FE",
        "END",
    ]
)


def _resnames(block: str) -> list[str]:
    return [line[17:20].strip() for line in block.splitlines() if line.startswith(("ATOM", "HETATM"))]


def test_dry_receptor_is_the_default_and_keeps_the_metal():
    excluded = receptor_excluded_resnames(keep_waters=False, keep_cofactors=False)

    assert _resnames(_without_resnames(_BLOCK, excluded)) == ["ASP", "ZN"]
    assert "ZN" not in excluded  # metals are part of the site, never optional


def test_waters_and_cofactors_ride_along_when_asked():
    both = receptor_excluded_resnames(keep_waters=True, keep_cofactors=True)
    waters_only = receptor_excluded_resnames(keep_waters=True, keep_cofactors=False)

    assert _resnames(_without_resnames(_BLOCK, both)) == ["ASP", "ZN", "HOH", "HEM"]
    assert _resnames(_without_resnames(_BLOCK, waters_only)) == ["ASP", "ZN", "HOH"]


def test_an_untyped_metal_is_left_out_even_when_keeping_everything():
    excluded = receptor_excluded_resnames(keep_waters=True, keep_cofactors=True)
    block = _BLOCK.replace("END", "HETATM    5 CD    CD A 701     -20.000 -20.000 -10.000  1.00 20.00          CD\nEND")

    assert _resnames(_without_resnames(block, excluded)) == ["ASP", "ZN", "HOH", "HEM"]


def test_flexible_residues_split_off_and_bad_keys_are_skipped(tmp_path):
    from pathlib import Path

    from amdockvs.docking.preparation.adtools import prepare_receptor_vina_pdbqt

    source = Path(__file__).resolve().parents[1] / "data" / "docking" / "1iep_receptorH.pdb"
    result = prepare_receptor_vina_pdbqt(
        source_path=source, output_path=tmp_path / "r.pdbqt",
        flexible_residues=["A:ARG:239", "A:PRO:227", "A:GLY:9999", "garbage"],
    )
    meta = result.metadata
    assert meta["flex_residues"] == ["A:ARG:239"]
    assert meta["flex_skipped"] == ["A:GLY:9999", "garbage", "A:PRO:227"]  # missing, malformed, nothing to rotate
    flex = Path(meta["flex_path"]).read_text()
    assert flex.startswith("BEGIN_RES ARG A 239") and flex.count("BEGIN_RES") == 1
    rigid = (tmp_path / "r.pdbqt").read_text()
    assert [l[12:16].strip() for l in rigid.splitlines() if l[17:26] == "ARG A 239"] == ["N", "HN", "C", "O"]


# --- orphan ANISOU records ---------------------------------------------------------------
# A dropped atom used to leave its ANISOU behind. The PDB parser refuses a file where one does
# not sit directly behind its own atom ("Duplicated ANISOU record or not directly after
# ATOM/HETATM"), so a receptor with tensors failed to prepare unless everything was kept.

_WITH_ANISOU = "\n".join(
    [
        "ATOM      1  N   ASP A 285      -3.256  -3.631  -1.685  1.00  0.00           N",
        "ANISOU    1  N   ASP A 285     2406   1835   2283    -51   -238    -17       N",
        "HETATM    2 ZN    ZN A 900     -37.000 -35.000 -12.000  1.00 20.00          ZN",
        "ANISOU    2 ZN    ZN A 900     1500   1500   1500      0      0      0      ZN",
        "TER",
        "HETATM    3  O   HOH A2001     -31.464 -37.112 -11.121  1.00 34.40           O",
        "ANISOU    3  O   HOH A2001     4000   4100   4200    100    -50     25       O",
        "HETATM    4  FE  HEM A 500     -30.000 -30.000 -10.000  1.00 20.00          FE",
        "ANISOU    4  FE  HEM A 500     2000   2000   2000      0      0      0      FE",
        "END",
    ]
)


def _orphans(block: str) -> list[str]:
    """The annotation records a PDB parser would reject: not directly behind their own atom."""
    found, serial = [], None
    for line in block.splitlines():
        if line.startswith(("ATOM", "HETATM")):
            serial = line[6:11].strip()
        elif line.startswith(("ANISOU", "SIGATM", "SIGUIJ")):
            if serial is None or line[6:11].strip() != serial:
                found.append(line[:26])
        else:
            serial = None
    return found


def test_a_dropped_residue_takes_its_anisou_record_with_it():
    excluded = receptor_excluded_resnames(keep_waters=False, keep_cofactors=False)

    filtered = _without_resnames(_WITH_ANISOU, excluded)

    assert _resnames(filtered) == ["ASP", "ZN"]
    assert [line[17:20].strip() for line in filtered.splitlines() if line.startswith("ANISOU")] == ["ASP", "ZN"]
    assert _orphans(filtered) == []  # what the parser used to refuse


def test_no_setting_leaves_an_orphan_annotation_behind():
    for keep_waters in (False, True):
        for keep_cofactors in (False, True):
            excluded = receptor_excluded_resnames(keep_waters=keep_waters, keep_cofactors=keep_cofactors)
            assert _orphans(_without_resnames(_WITH_ANISOU, excluded)) == []


def test_sigatm_and_siguij_follow_their_atom_too():
    block = _WITH_ANISOU.replace(
        "HETATM    4  FE  HEM A 500     -30.000 -30.000 -10.000  1.00 20.00          FE",
        "\n".join([
            "SIGATM    3  O   HOH A2001      0.020  0.030  0.040  0.00  0.00           O",
            "SIGUIJ    3  O   HOH A2001       20     30     40      5      3      1       O",
            "HETATM    4  FE  HEM A 500     -30.000 -30.000 -10.000  1.00 20.00          FE",
        ]),
    )
    excluded = receptor_excluded_resnames(keep_waters=False, keep_cofactors=True)

    filtered = _without_resnames(block, excluded)

    assert "HOH" not in filtered and "SIGATM" not in filtered and "SIGUIJ" not in filtered
    assert _resnames(filtered) == ["ASP", "ZN", "HEM"] and _orphans(filtered) == []


def test_an_annotation_that_does_not_match_its_atom_is_dropped():
    """A tensor whose serial does not belong to the atom before it is already an orphan: it goes,
    rather than being carried into the file the parser reads."""
    block = _WITH_ANISOU.replace(
        "ANISOU    1  N   ASP A 285     2406   1835   2283    -51   -238    -17       N",
        "ANISOU   99  N   ASP A 285     2406   1835   2283    -51   -238    -17       N",
    )
    filtered = _without_resnames(block, receptor_excluded_resnames(keep_waters=False, keep_cofactors=False))

    assert _resnames(filtered) == ["ASP", "ZN"] and _orphans(filtered) == []


def test_a_receptor_with_tensors_prepares_with_the_default_dry_settings(tmp_path):
    """End to end (F2-01): a water's ANISOU in the source file used to sink the whole preparation."""
    from pathlib import Path

    from amdockvs.docking.preparation.adtools import prepare_receptor_vina_pdbqt

    source = Path(__file__).resolve().parents[1] / "data" / "docking" / "1iep_receptorH.pdb"
    lines = source.read_text().splitlines(True)
    # Two of them: dropping the atoms leaves their tensors adjacent, which is what the parser
    # reports as a duplicated ANISOU. One orphan after an unrelated atom slips past it.
    water = [
        "HETATM 4414  O   HOH A2001     -31.464 -37.112 -11.121  1.00 34.40           O\n",
        "ANISOU 4414  O   HOH A2001     4000   4100   4200    100    -50     25       O\n",
        "HETATM 4415  O   HOH A2002     -29.112 -35.004 -13.880  1.00 31.20           O\n",
        "ANISOU 4415  O   HOH A2002     3800   3900   4000     90    -40     20       O\n",
    ]
    with_water = tmp_path / "receptor_with_waters.pdb"
    with_water.write_text("".join(lines[:-1] + water + lines[-1:]))

    result = prepare_receptor_vina_pdbqt(
        source_path=with_water, output_path=tmp_path / "r.pdbqt",
        exclude_resnames=receptor_excluded_resnames(keep_waters=False, keep_cofactors=False),
    )

    prepared = Path(result.artifact.path).read_text()
    assert "HOH" not in prepared and prepared.count("ATOM") > 100
