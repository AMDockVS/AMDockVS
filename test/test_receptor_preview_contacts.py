"""Structural waters and coordinated metals in the receptor scan (the numpy distance checks)."""

from amdockvs.io.receptor_preview import scan_receptor_structure


def _atom(serial, name, resname, resseq, x, y, z, element, record="HETATM"):
    return f"{record:<6}{serial:>5} {name:<4} {resname:>3} A{resseq:>4}    {x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00          {element:>2}"


def test_structural_water_and_coordinated_zinc(tmp_path):
    lines = [
        _atom(1, "N", "SER", 1, 0.0, 0.0, 0.0, "N", "ATOM"),
        _atom(2, "OG", "SER", 1, 3.0, 0.0, 0.0, "O", "ATOM"),
        _atom(3, "ND1", "HIS", 2, 20.0, 2.0, 0.0, "N", "ATOM"),
        _atom(4, "NE2", "HIS", 2, 20.0, -2.0, 0.0, "N", "ATOM"),
        _atom(5, "O1", "LIG", 10, 9.0, 0.0, 0.0, "O"),
        _atom(6, "C1", "LIG", 10, 10.0, 0.0, 0.0, "C"),
        _atom(7, "O", "HOH", 100, 6.0, 0.0, 0.0, "O"),  # 3.0 Å from SER OG and from LIG O1
        _atom(8, "O", "HOH", 101, 40.0, 40.0, 40.0, "O"),  # far from everything
        _atom(9, "ZN", "ZN", 200, 20.0, 0.0, 0.0, "ZN"),  # 2.0 Å from both HIS nitrogens
    ]
    path = tmp_path / "toy.pdb"
    path.write_text("\n".join(lines) + "\nEND\n")
    components = {c["selector"]: c for c in scan_receptor_structure(path)["components"]}
    assert components["HOH:A:100"]["is_structural"] is True
    assert components["HOH:A:101"]["is_structural"] is False
    assert components["ZN:A:200"]["is_coordinated"] is True


def test_bulk_import_builds_the_same_binding_sites_as_the_dialog(tmp_path):
    from dataclasses import replace

    from amdockvs.core.vocab import MoleculeType
    from amdockvs.io.payloads import ImportBatchPayload
    from amdockvs.io.receptor_preview import ReceptorImportOptions, build_receptor_import_maps
    from amdockvs.io.transformers.structures import _materialize_structure_rows

    lines = [
        _atom(1, "N", "SER", 1, 0.0, 0.0, 0.0, "N", "ATOM"),
        _atom(2, "CA", "SER", 1, 1.5, 0.0, 0.0, "C", "ATOM"),
        _atom(3, "OG", "SER", 1, 3.0, 0.0, 0.0, "O", "ATOM"),
        _atom(4, "O1", "LIG", 10, 9.0, 0.0, 0.0, "O"),
        _atom(5, "C1", "LIG", 10, 10.0, 0.0, 0.0, "C"),
        _atom(6, "C2", "LIG", 10, 11.0, 1.0, 0.0, "C"),
    ]
    source = tmp_path / "rec.pdb"
    source.write_text("\n".join(lines) + "\nEND\n")
    options = replace(ReceptorImportOptions(), create_binding_sites_from_components=True)

    def first_row(build_specs: bool) -> dict:
        storage = tmp_path / f"molecules_{build_specs}"
        storage.mkdir()
        patches, specs = build_receptor_import_maps([source], base_options=options, build_specs=build_specs)
        key = str(source.resolve())
        batch = ImportBatchPayload(
            kind="receptor", file_path=source, storage_dir=storage, input_format="pdb", primary_role="receptor",
            molecule_kind=MoleculeType.PROTEIN, extra_data_patch=patches[key], binding_site_specs=specs.get(key, []),
        )
        return list(_materialize_structure_rows(batch=batch, entries=[]))[0]

    dialog, bulk = first_row(True), first_row(False)
    assert dialog["binding_site_specs"]
    assert bulk["binding_site_specs"] == dialog["binding_site_specs"]
