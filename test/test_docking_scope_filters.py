"""The virtual filter keys must never reach SQL — leaking one gives "no such column: prepared"."""

from amdockvs.docking.repository import _entity_scope_filters


def test_virtual_keys_are_stripped():
    kind, sql_filters, prepared, grid = _entity_scope_filters(
        "ligand",
        {
            "molecule_type": "small_molecule",
            "prepared": True,
            "prepared_engine_key": "ad4",
            "prepared_ad4": True,
            "prepared_engine": True,
            "grid_engine": False,
            "grid_ad4": False,
        },
        engine="ad4",
    )
    assert kind == "ligand"
    assert prepared is True and grid is False
    assert sql_filters == {"molecule_type": "small_molecule", "excluded": False, "is_ligand": True}


def test_receptor_defaults():
    _, sql_filters, prepared, grid = _entity_scope_filters("receptor", None, engine="ad4")
    assert prepared is None and grid is None
    assert sql_filters == {"excluded": False, "is_receptor": True}
