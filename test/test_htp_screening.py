from rdkit import Chem

from amdockvs.htp.screening import HTPFilterConfig, evaluate_mol


def _verdicts(smiles_names, config, **kwargs):
    """Stream `(name, reason)` the way a real caller does: one verdict at a time, nothing kept."""
    return {
        name: evaluate_mol(Chem.MolFromSmiles(smi), config, **kwargs)[0]
        for name, smi in smiles_names
    }


def test_invalid_mol_is_rejected():
    assert evaluate_mol(None, HTPFilterConfig())[0] == "invalid"
    assert evaluate_mol(Chem.MolFromSmiles("CCO"), HTPFilterConfig())[0] is None


def test_substructure_include_exclude():
    verdicts = _verdicts(
        [("benzene", "c1ccccc1"), ("ethanol", "CCO"), ("nitro", "c1ccccc1[N+](=O)[O-]")],
        HTPFilterConfig(include_smarts=("c1ccccc1",), exclude_smarts=("[N+](=O)[O-]",)),
    )
    assert verdicts == {"benzene": None, "ethanol": "include_smarts", "nitro": "exclude_smarts"}


def test_property_range_and_ro5():
    verdicts = _verdicts(
        [("ethanol", "CCO"), ("aspirin", "CC(=O)Oc1ccccc1C(=O)O")],
        HTPFilterConfig(property_ranges={"mw": (100.0, 500.0)}),
    )
    assert verdicts == {"ethanol": "property:mw", "aspirin": None}

    ro5 = _verdicts([("c20", "C" * 20), ("ethanol", "CCO")], HTPFilterConfig(max_ro5_violations=0))
    assert ro5["c20"] == "ro5"  # logp>5 -> ro5 violation
    assert ro5["ethanol"] is None


def test_pains_filter():
    # rhodanine is a textbook PAINS; benzene is clean.
    verdicts = _verdicts(
        [("benzene", "c1ccccc1"), ("rhodanine", "O=C1CSC(=S)N1")],
        HTPFilterConfig(exclude_pains=True),
    )
    assert verdicts == {"benzene": None, "rhodanine": "pains"}


def test_qsar_threshold_gate():
    cfg = HTPFilterConfig(qsar_threshold=100.0, qsar_op=">=")
    # predict = mw; keep mw >= 100 -> only aspirin
    verdicts = _verdicts(
        [("small", "CCO"), ("big", "CC(=O)Oc1ccccc1C(=O)O")],
        cfg,
        qsar_predict=lambda d: float(d["mw"]),
    )
    assert verdicts == {"small": "qsar", "big": None}

    # Descriptors and the predicted value come back with the verdict, so a caller that wants
    # to record them never re-computes.
    reason, descriptors, qsar_value = evaluate_mol(
        Chem.MolFromSmiles("CC(=O)Oc1ccccc1C(=O)O"), cfg, qsar_predict=lambda d: float(d["mw"])
    )
    assert reason is None and descriptors["mw"] == qsar_value
