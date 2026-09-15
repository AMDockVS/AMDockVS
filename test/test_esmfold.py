"""ESMFold core without the network: FASTA parsing, request shape, response -> mmCIF round trip."""
from __future__ import annotations

import numpy as np
import pytest

from amdockvs.chemistry.tools.esmfold import (
    build_fold_request,
    chain_input,
    protein_chains,
    save_prediction,
    sequence_from_structure,
)
from amdockvs.io.parsers.readers import parse_fasta


def test_parse_fasta_records_and_headerless_text():
    text = ">sp|P1 first\nMKT AYI\n*\n\n>second\nGG:AA\n>empty\n"
    assert parse_fasta(text) == [("sp|P1", "MKTAYI"), ("second", "GG:AA")]
    assert parse_fasta("mkt\nayi\n", stem="pasted") == [("pasted", "MKTAYI")]


def test_request_is_generic_over_chain_types():
    chains = protein_chains("mkt:gga")
    assert [(c["id"], c["sequence"], c["type"]) for c in chains] == [("A", "MKT", "protein"), ("B", "GGA", "protein")]
    assert "msa" not in chain_input("dna", "C", sequence="ACGT")
    assert chain_input("ligand", "D", ccd=["ATP"]) == {"smiles": None, "id": "D", "ccd": ["ATP"], "type": "ligand"}
    with pytest.raises(ValueError):
        chain_input("ligand", "E")
    body = build_fold_request(chains, model="esmfold2-2026-05")
    assert body["include_pae"] is True and body["lm_mask_pct"] == 0.0
    assert body["all_atom_input"]["sequences"] == chains


def test_complex_response_writes_structure_with_plddt(tmp_path):
    positions = [[float(i), 0.0, 0.0] for i in range(9)]
    data = {
        "plddt": [0.9, 0.4],
        "ptm": 0.5,
        "pae": [[0.0, 1.0], [1.0, 0.0]],
        "tokens_used": 1,
        "complex": {
            "sequence": ["GLY", "ALA"],
            "chain_id": [0, 0],
            "token_to_atoms": [[0, 4], [4, 9]],
            "atom_names": ["N", "CA", "C", "O", "N", "CA", "C", "O", "CB"],
            "atom_elements": ["N", "C", "C", "O", "N", "C", "C", "O", "C"],
            "atom_positions": positions,
            "metadata": {"chain_lookup": {"0": "A"}},
        },
    }
    metrics, files = save_prediction(data, tmp_path / "model.cif", model="esmfold2-fast-2026-05")
    assert metrics["plddt_mean"] == 65.0 and metrics["ptm"] == 0.5
    assert np.load(files["pae"]).shape == (2, 2)
    assert sequence_from_structure(tmp_path / "model.cif") == "GA"

    import gemmi

    structure = gemmi.read_structure(str(tmp_path / "model.cif"))
    assert [round(res[0].b_iso) for res in structure[0]["A"]] == [90, 40]
