"""An mmCIF names the parts of an assembly (label_asym_id), not its chains: the ligand and the
waters of the chain must still be there when the biological assembly is asked for."""
from amdockvs.io.receptor_preview import ReceptorImportOptions, build_receptor_import_preview, scan_receptor_structure

ATOMS = [  # group, atom, residue, label_asym, entity, label_seq, auth_seq, x, y, z, element
    ("ATOM", "N", "ALA", "A", 1, 1, 1, 0.0, 0.0, 0.0, "N"), ("ATOM", "CA", "ALA", "A", 1, 1, 1, 1.5, 0.0, 0.0, "C"),
    ("ATOM", "C", "ALA", "A", 1, 1, 1, 2.0, 1.4, 0.0, "C"), ("ATOM", "O", "ALA", "A", 1, 1, 1, 1.2, 2.3, 0.0, "O"),
    *[("HETATM", f"C{i}", "JXM", "B", 2, ".", 900, 5.0 + i, 5.0, 5.0 + (i % 2), "C") for i in range(1, 13)],
    ("HETATM", "O", "HOH", "C", 3, ".", 2001, 9.0, 9.0, 9.0, "O"),
]
CIF = """data_T
_pdbx_struct_assembly.id 1
_pdbx_struct_assembly.details author_defined_assembly
_pdbx_struct_assembly.method_details PISA
_pdbx_struct_assembly.oligomeric_details monomeric
_pdbx_struct_assembly.oligomeric_count 1
loop_
_pdbx_struct_assembly_gen.assembly_id
_pdbx_struct_assembly_gen.oper_expression
_pdbx_struct_assembly_gen.asym_id_list
1 1 A,B,C
loop_
_pdbx_struct_oper_list.id
_pdbx_struct_oper_list.type
_pdbx_struct_oper_list.matrix[1][1]
_pdbx_struct_oper_list.matrix[1][2]
_pdbx_struct_oper_list.matrix[1][3]
_pdbx_struct_oper_list.vector[1]
_pdbx_struct_oper_list.matrix[2][1]
_pdbx_struct_oper_list.matrix[2][2]
_pdbx_struct_oper_list.matrix[2][3]
_pdbx_struct_oper_list.vector[2]
_pdbx_struct_oper_list.matrix[3][1]
_pdbx_struct_oper_list.matrix[3][2]
_pdbx_struct_oper_list.matrix[3][3]
_pdbx_struct_oper_list.vector[3]
1 'identity operation' 1 0 0 0 0 1 0 0 0 0 1 0
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.type_symbol
_atom_site.label_atom_id
_atom_site.label_alt_id
_atom_site.label_comp_id
_atom_site.label_asym_id
_atom_site.label_entity_id
_atom_site.label_seq_id
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
_atom_site.occupancy
_atom_site.B_iso_or_equiv
_atom_site.auth_seq_id
_atom_site.auth_asym_id
_atom_site.pdbx_PDB_model_num
""" + "".join(
    f"{group} {n} {element} {atom} . {residue} {asym} {entity} {seq} {x} {y} {z} 1.0 10.0 {auth} A 1\n"
    for n, (group, atom, residue, asym, entity, seq, auth, x, y, z, element) in enumerate(ATOMS, 1)
)


def test_the_biological_assembly_of_a_cif_keeps_what_sits_on_its_chains(tmp_path) -> None:
    path = tmp_path / "t.cif"
    path.write_text(CIF)
    scan = scan_receptor_structure(path)

    assert scan["assembly_chains"] == {"1": ["A"]}
    kept = build_receptor_import_preview(scan, ReceptorImportOptions(selected_assembly="1"))
    whole = build_receptor_import_preview(scan, ReceptorImportOptions(use_biological_assembly=False))
    assert kept["workflow"]["ligand_candidates"] == whole["workflow"]["ligand_candidates"] == ["JXM:A:900"]
