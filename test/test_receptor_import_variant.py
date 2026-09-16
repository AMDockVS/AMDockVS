"""Two receptor imports of one file with different options must not share a storage key."""
from pathlib import Path

from amdockvs.core.paths import molecule_storage_key
from amdockvs.io.receptor_preview import ReceptorImportOptions
from amdockvs.io.transformers.structures import _receptor_import_variant


def test_import_options_change_the_storage_key() -> None:
    source = Path(__file__)
    variants = [
        ReceptorImportOptions(selected_chain_ids=("A",)),
        ReceptorImportOptions(selected_chain_ids=("B",)),
        ReceptorImportOptions(selected_chain_ids=("A", "B")),
        ReceptorImportOptions(selected_chain_ids=("A",), selected_assembly="1"),
        ReceptorImportOptions(selected_chain_ids=("A",), remove_cofactors=True),
    ]
    keys = {
        molecule_storage_key("receptor", source, 0, _receptor_import_variant(options))
        for options in variants
    }
    assert len(keys) == len(variants)


def test_no_options_keeps_the_plain_key() -> None:
    source = Path(__file__)
    assert _receptor_import_variant(None) == ""
    assert molecule_storage_key("ligand", source, 3, "") == molecule_storage_key("ligand", source, 3)
