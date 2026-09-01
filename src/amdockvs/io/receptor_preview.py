from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, replace
from math import sqrt
from pathlib import Path
from typing import Any

import gemmi

from amdockvs.configuration import DEFAULT_BINDING_SITE_BOX_SIZE


def _load_hetero_codes() -> dict[str, set[str]]:
    """HET classification codes live in data/hetero_codes.json so they can be curated
    without a code change (e.g. adding a crystallization additive like PO4)."""
    data = json.loads((Path(__file__).resolve().parent.parent / "data" / "hetero_codes.json").read_text())
    return {key: {str(code).upper() for code in value} for key, value in data.items() if not key.startswith("_")}


_HETERO_CODES = _load_hetero_codes()


def hetero_codes(kind: str) -> frozenset[str]:
    """The curated HET codes of one class ("water", "cofactor", "ion", ...).

    Receptor preparation filters by the same lists the import panel classifies with, so a
    residue can't be a cofactor here and something else there.
    """
    return frozenset(_HETERO_CODES.get(str(kind), ()))


_WATER_CODES = _HETERO_CODES["water"]
_COORDINATION_METAL_CODES = _HETERO_CODES["coordination_metal"]
_ION_CODES = _HETERO_CODES["ion"]
_ADDITIVE_CODES = _HETERO_CODES["additive"]
_COFACTOR_CODES = _HETERO_CODES["cofactor"]
_AMINO_ACIDS = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL", "MSE",
}
_NUCLEOTIDE_CODES = {"A", "C", "G", "U", "T", "DA", "DC", "DG", "DT", "DU"}


@dataclass(frozen=True)
class ReceptorImportOptions:
    use_biological_assembly: bool = True
    remove_non_structural_waters: bool = True
    create_binding_sites_from_components: bool = False
    remove_cofactors: bool = False
    remove_altloc: bool = True
    import_mode: str = "receptor"
    binding_site_box_size: float = DEFAULT_BINDING_SITE_BOX_SIZE
    selected_cocrystal_key: str = ""
    activity_text: str = ""
    # Chains the user chose to keep. Empty = keep all (e.g. drop chain B of a dimer, keep A).
    selected_chain_ids: tuple[str, ...] = ()
    # Biological assembly name to build; "" = asymmetric unit (no transform).
    selected_assembly: str = ""
    # Cocrystal ligand selectors to keep as references; None = all candidates (drop artifact copies).
    selected_reference_ligands: tuple[str, ...] | None = None


def preview_receptor_import(file_path: str | Path, options: ReceptorImportOptions) -> dict[str, Any]:
    return build_receptor_import_preview(scan_receptor_structure(file_path), options)


def scan_receptor_structure(file_path: str | Path) -> dict[str, Any]:
    path = Path(file_path).expanduser().resolve()
    suffix = path.suffix.lower()
    if suffix in {".pdb", ".cif", ".mmcif"}:
        return _scan_with_gemmi(path)
    if suffix == ".pdbqt":
        return _scan_pdbqt_fallback(path)
    return _unsupported_scan(path)


def build_receptor_import_maps(
    file_paths,
    *,
    base_options: ReceptorImportOptions,
    per_file: dict[str, dict] | None = None,
    scans: dict[str, dict] | None = None,
    build_specs: bool = True,
) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """Raw receptor-import intent -> the (extra_data_patch, binding_site_specs) maps the import job
    consumes. This is the single owner of that assembly: the UI hands over raw selections and the
    API calls this, so headless and UI imports produce identical results (same preview builder).

    ``per_file[path]``: optional per-file selections (selected_chain_ids, selected_assembly,
    selected_reference_ligands, selected_cocrystal_key, activity). ``scans[path]``: a pre-computed
    scan (the UI's cache) to skip re-reading the file; scanned on demand when absent.
    ``build_specs=False`` emits the minimal patch without scanning — bulk imports that skip
    per-file binding-site derivation on the submit thread (the worker still preps each file).
    """
    per_file = per_file or {}
    scans = scans or {}
    extra_data_patch_by_file: dict[str, dict] = {}
    binding_site_specs_by_file: dict[str, list[dict]] = {}
    for raw_path in file_paths:
        path = str(Path(raw_path).expanduser().resolve())
        if not build_specs:
            extra_data_patch_by_file[path] = {
                "structure": {
                    "import_profile": {
                        "use_biological_assembly": bool(base_options.use_biological_assembly),
                        "remove_non_structural_waters": bool(base_options.remove_non_structural_waters),
                        "create_binding_sites_from_components": bool(base_options.create_binding_sites_from_components),
                        "remove_cofactors": bool(base_options.remove_cofactors),
                        "remove_altloc": bool(base_options.remove_altloc),
                        "binding_site_box_size": float(base_options.binding_site_box_size),
                    }
                },
                "workflow": {
                    "import_mode": str(base_options.import_mode or "receptor"),
                    "selected_cocrystal_key": "",
                    "activity": "",
                    "ligand_candidates": [],
                },
            }
            continue
        scan = scans.get(path) or scans.get(str(raw_path)) or scan_receptor_structure(path)
        state = per_file.get(path) or per_file.get(str(raw_path)) or {}
        refs = state.get("selected_reference_ligands")
        options = replace(
            base_options,
            selected_cocrystal_key=str(state.get("selected_cocrystal_key", "")),
            activity_text=str(state.get("activity", "")),
            selected_chain_ids=tuple(state.get("selected_chain_ids") or ()),
            selected_assembly=str(state.get("selected_assembly", "")),
            selected_reference_ligands=None if refs is None else tuple(refs),
        )
        preview = build_receptor_import_preview(scan, options)
        extra_data_patch_by_file[path] = {
            "__scan": dict(scan or {}),
            "structure": dict(preview.get("structure") or {}),
            "precheck": dict(preview.get("precheck") or {}),
            "workflow": dict(preview.get("workflow") or {}),
        }
        binding_site_specs_by_file[path] = list(preview.get("binding_site_specs") or [])
    return extra_data_patch_by_file, binding_site_specs_by_file


def build_receptor_import_preview(scan: dict[str, Any], options: ReceptorImportOptions) -> dict[str, Any]:
    path = Path(str(scan.get("file_path") or "")).expanduser().resolve()
    if not bool(scan.get("supported", False)):
        return _unsupported_preview(path, options)

    chains = [dict(item) for item in list(scan.get("chains") or [])]
    components = deepcopy(list(scan.get("components") or []))
    assemblies = list(scan.get("assemblies") or [])
    altloc_values = list(scan.get("altloc_values") or [])

    # Unit (asymmetric unit vs a biological assembly) narrows the candidate chains; the chain
    # selection then keeps a subset of those — everything (ligands/metals/waters) on dropped
    # chains goes too, so downstream only offers what will actually be kept.
    all_chain_labels = [str(chain.get("label")) for chain in chains]
    assembly_chains = {str(name): [str(label) for label in labels] for name, labels in dict(scan.get("assembly_chains") or {}).items()}
    chosen_assembly = str(options.selected_assembly or "")
    if not chosen_assembly and options.use_biological_assembly and assemblies:
        chosen_assembly = str(assemblies[0])  # legacy: the old bool checkbox meant "first assembly"
    if chosen_assembly and chosen_assembly in assembly_chains:
        unit_chains = [label for label in all_chain_labels if label in set(assembly_chains[chosen_assembly])]
    else:
        chosen_assembly = ""  # asymmetric unit (or unknown assembly) → all chains
        unit_chains = list(all_chain_labels)
    selected_chain_ids = [str(chain_id) for chain_id in (options.selected_chain_ids or ())]
    kept_chains = _resolve_kept_chains(unit_chains, selected_chain_ids)
    if kept_chains != set(all_chain_labels):
        chains = [chain for chain in chains if str(chain.get("label")) in kept_chains]
        components = [comp for comp in components if str(comp.get("chain_id") or "") in kept_chains]
    missing_residues = int(scan.get("missing_residues") or 0)
    missing_atoms = int(scan.get("missing_atoms") or 0)
    atom_count = int(scan.get("atom_count") or 0)

    for component in components:
        if component["component_class"] == "water":
            component["is_selected"] = bool(component.get("is_structural")) if options.remove_non_structural_waters else True

    # Reference-ligand selection: which cocrystals to keep (None = all). Artifact copies get
    # dropped here, so they neither become references nor spawn a binding site.
    candidate_selectors = [component["selector"] for component in components if component["component_class"] == "ligand"]
    if options.selected_reference_ligands is None:
        reference_ligands = list(candidate_selectors)
    else:
        wanted = set(options.selected_reference_ligands)
        reference_ligands = [selector for selector in candidate_selectors if selector in wanted]

    binding_site_specs = _build_binding_site_specs(components, options, reference_ligands=reference_ligands)
    workflow = _build_workflow_payload(components, options)
    workflow["reference_ligands"] = list(reference_ligands)
    status, messages = _build_status(
        atom_count=atom_count,
        missing_residues=missing_residues,
        missing_atoms=missing_atoms,
        altloc_values=altloc_values,
    )

    ligand_labels = [component["label"] for component in components if component["component_class"] == "ligand"]
    metal_labels = [component["label"] for component in components if component["component_class"] == "metal"]
    water_components = [component for component in components if component["component_class"] == "water"]
    waters_structural = sum(1 for component in water_components if component.get("is_structural"))
    waters_selected = sum(1 for component in water_components if component.get("is_selected"))
    selected_assembly = f"BioAsm {chosen_assembly}" if chosen_assembly else "Complete"

    structure = {
        "import_profile": {
            "use_biological_assembly": bool(chosen_assembly),
            "remove_non_structural_waters": bool(options.remove_non_structural_waters),
            "waters_detected": len(water_components),
            "waters_structural": waters_structural,
            "waters_selected": waters_selected,
            "create_binding_sites_from_components": bool(options.create_binding_sites_from_components),
            "remove_cofactors": bool(options.remove_cofactors),
            "remove_altloc": bool(options.remove_altloc),
            "selected_chain_ids": selected_chain_ids,
            "selected_assembly": chosen_assembly,
            "selected_reference_ligands": None if options.selected_reference_ligands is None else list(options.selected_reference_ligands),
        },
        "assembly": {
            "mode": "biological" if chosen_assembly else "complete",
            "selected": selected_assembly,
            "available_ids": assemblies,
        },
        "chains": chains,
        "components": {
            "ligands": [_component_payload(component) for component in components if component["component_class"] == "ligand"],
            "metals": [_component_payload(component) for component in components if component["component_class"] == "metal"],
            "ions": [_component_payload(component) for component in components if component["component_class"] == "ion"],
            "cofactors": [_component_payload(component) for component in components if component["component_class"] == "cofactor"],
            "additives": [_component_payload(component) for component in components if component["component_class"] == "additive"],
            "waters": {
                "count": len(water_components),
                "structural_count": waters_structural,
                "selected_count": waters_selected,
                "selected_residues": [component["selector"] for component in water_components if component.get("is_selected")],
            },
        },
        "altloc": {
            "present": bool(altloc_values),
            "values": altloc_values,
            "selection_policy": "A_or_highest_occupancy" if options.remove_altloc else "keep_all",
        },
    }
    precheck = {
        "missing_residues": {"count": missing_residues, "present": missing_residues > 0},
        "missing_atoms": {"count": missing_atoms, "present": missing_atoms > 0},
        "status": status,
        "messages": messages,
        "valid_for_binding_site": bool(ligand_labels or metal_labels),
    }
    summary = {
        "name": str(scan.get("name") or path.stem),
        "assembly": selected_assembly,
        "chains": ",".join(chain["label"] for chain in chains) or "-",
        "ligands": len(ligand_labels),
        "ligand_labels": ligand_labels,
        "metals": len(metal_labels),
        "metal_labels": metal_labels,
        "waters": waters_selected,
        "status": status,
        "selected_cocrystal_key": workflow.get("selected_cocrystal_key", ""),
    }
    return {
        "file_path": str(path),
        "name": str(scan.get("name") or path.stem),
        "structure": structure,
        "precheck": precheck,
        "workflow": workflow,
        "binding_site_specs": binding_site_specs,
        "summary": summary,
    }


def write_processed_receptor(
    source_file: str | Path,
    output_path: str | Path,
    *,
    scan: dict[str, Any],
    options: ReceptorImportOptions,
) -> dict[str, Any]:
    path = Path(source_file).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    suffix = path.suffix.lower()
    if suffix not in {".pdb", ".cif", ".mmcif"}:
        if path != output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(path.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
        return {"selected_chain_ids": [], "removed_component_selectors": []}

    structure = gemmi.read_structure(str(path))
    if len(structure) == 0:
        raise ValueError(f"No models found in receptor structure: {path}")
    assembly_names = {str(assembly.name) for assembly in structure.assemblies}
    chosen_assembly = str(options.selected_assembly or "")
    if not chosen_assembly and options.use_biological_assembly and structure.assemblies:
        chosen_assembly = str(structure.assemblies[0].name)  # legacy bool → first assembly
    if chosen_assembly and chosen_assembly in assembly_names:
        structure.transform_to_assembly(chosen_assembly, gemmi.HowToNameCopiedChain.Short)
    model = structure[0]

    workflow = build_receptor_import_preview(scan, options).get("workflow") or {}
    selected_cocrystal_key = str(workflow.get("selected_cocrystal_key") or "")
    selected_chain_ids = _selected_chain_ids(scan, selected_cocrystal_key, options.selected_chain_ids)
    removable_components = _components_to_remove(scan, options, selected_cocrystal_key)
    component_keys = {tuple(item) for item in removable_components}

    processed = gemmi.Structure()
    processed.cell = structure.cell
    processed.spacegroup_hm = structure.spacegroup_hm
    processed_model = gemmi.Model("1")

    for chain in model:
        chain_name = str(chain.name or "-")
        if selected_chain_ids and chain_name not in selected_chain_ids:
            continue
        new_chain = gemmi.Chain(chain.name)
        for residue in chain:
            resname = str(residue.name or "").strip().upper()
            resseq = str(residue.seqid.num)
            icode = str(residue.seqid.icode or "").strip()
            residue_key = (resname, chain_name, resseq, icode)
            if residue_key in component_keys:
                continue
            # What survives is decided by _components_to_remove() above (that is where the
            # "Remove cofactors" / "Remove non-structural waters" options are applied). Here we
            # only classify by resname, never by residue_key: a biological assembly renames the
            # copied chains, so a copy's key matches nothing and a key-based keep-list would drop
            # every metal in the copies. Anything the scan could not classify came out "ligand",
            # so it is already in component_keys and falls through to the last `continue`.
            if _is_polymer_residue(resname) or resname in _COORDINATION_METAL_CODES:
                new_chain.add_residue(residue.clone())
            elif resname in _WATER_CODES or resname in _COFACTOR_CODES:
                new_chain.add_residue(residue.clone())
        if len(new_chain):
            processed_model.add_chain(new_chain)

    if options.remove_altloc:
        processed_model.remove_alternative_conformations()
    processed.add_model(processed_model)
    output.parent.mkdir(parents=True, exist_ok=True)
    processed.write_pdb(str(output))
    return {
        "selected_chain_ids": selected_chain_ids,
        "selected_cocrystal_key": str(workflow.get("selected_cocrystal_key") or ""),
        "retained_structural_waters": [
            str(component.get("selector") or "")
            for component in list(scan.get("components") or [])
            if str(component.get("component_class") or "") == "water"
            and bool(component.get("is_structural"))
            and (not selected_chain_ids or str(component.get("chain_id") or "") in selected_chain_ids)
        ],
        "retained_metals": [
            str(component.get("selector") or "")
            for component in list(scan.get("components") or [])
            if str(component.get("component_class") or "") == "metal"
            and (not selected_chain_ids or str(component.get("chain_id") or "") in selected_chain_ids)
        ],
    }


def extract_component_to_pdb(
    source_file: str | Path,
    output_path: str | Path,
    *,
    selector: str,
    use_biological_assembly: bool = False,
) -> dict[str, Any] | None:
    path = Path(source_file).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    suffix = path.suffix.lower()
    if suffix not in {".pdb", ".cif", ".mmcif"}:
        return None
    selector_parts = _parse_selector(selector)
    if selector_parts is None:
        return None
    resname, chain_name, resseq, icode = selector_parts
    structure = gemmi.read_structure(str(path))
    if len(structure) == 0:
        return None
    if use_biological_assembly and structure.assemblies:
        structure.transform_to_assembly(structure.assemblies[0].name, gemmi.HowToNameCopiedChain.Short)
    model = structure[0]

    output_structure = gemmi.Structure()
    output_structure.cell = structure.cell
    output_structure.spacegroup_hm = structure.spacegroup_hm
    output_model = gemmi.Model("1")
    atom_count = 0
    center = (0.0, 0.0, 0.0)

    for chain in model:
        if str(chain.name or "-") != chain_name:
            continue
        new_chain = gemmi.Chain(chain.name)
        for residue in chain:
            if (
                str(residue.name or "").strip().upper() == resname
                and str(residue.seqid.num) == resseq
                and str(residue.seqid.icode or "").strip() == icode
            ):
                new_chain.add_residue(residue.clone())
                atoms = list(residue)
                atom_count = len(atoms)
                center = _gemmi_center(atoms)
                break
        if len(new_chain):
            output_model.add_chain(new_chain)
            break
    if len(output_model) == 0:
        return None
    output_structure.add_model(output_model)
    output.parent.mkdir(parents=True, exist_ok=True)
    output_structure.write_pdb(str(output))
    return {
        "resname": resname,
        "chain_id": chain_name,
        "resseq": resseq,
        "icode": icode,
        "atom_count": atom_count,
        "center": center,
    }


def _parse_modres_codes(lines: list[str]) -> set[str]:
    """Modified-residue HET codes declared by MODRES records (PDB). These are covalently
    part of the chain (e.g. SUI in 1gvt), not free cocrystal ligands, so they must not be
    offered as ligand candidates — prep (meeko) substitutes/strips them."""
    codes: set[str] = set()
    for line in lines:
        if line.startswith("MODRES"):
            code = line[12:15].strip().upper()
            if code:
                codes.add(code)
    return codes


def _scan_with_gemmi(path: Path) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    modres_codes = _parse_modres_codes(lines)
    structure = gemmi.read_structure(str(path))
    model = structure[0] if len(structure) else None
    if model is None:
        return _unsupported_scan(path)

    chains_map: dict[str, dict[str, Any]] = {}
    grouped_components: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    polymer_points: list[tuple[float, float, float]] = []
    polymer_polar_points: list[tuple[float, float, float]] = []
    altloc_values: set[str] = set()

    for chain in model:
        chain_name = str(chain.name or "-")
        for residue in chain:
            resname = str(residue.name or "").strip().upper()
            atoms = list(residue)
            if not atoms:
                continue
            for atom in atoms:
                altloc = str(atom.altloc or "").replace("\x00", "").strip()
                if altloc:
                    altloc_values.add(altloc)

            is_polymer = _is_polymer_residue(resname)
            if is_polymer:
                payload = chains_map.setdefault(
                    chain_name,
                    {"label": chain_name, "polymer_type": _polymer_type_for_resname(resname), "residues": set()},
                )
                payload["residues"].add((resname, str(residue.seqid.num), str(residue.seqid.icode or "").strip()))
                for atom in atoms:
                    polymer_points.append((atom.pos.x, atom.pos.y, atom.pos.z))
                    if str(atom.element.name or "").upper() in {"O", "N", "S"}:
                        polymer_polar_points.append((atom.pos.x, atom.pos.y, atom.pos.z))
                continue

            resseq = str(residue.seqid.num)
            icode = str(residue.seqid.icode or "").strip()
            selector = f"{resname}:{chain_name}:{resseq}{icode}"
            key = (resname, chain_name, resseq, icode)
            center = _gemmi_center(atoms)
            component_class = _classify_component(resname, [atom.element.name.upper() for atom in atoms])
            # MODRES residues are modified chain residues, not free ligands — keep them out
            # of the ligand candidates so prep handles them instead of docking against them.
            if resname in modres_codes:
                component_class = "modified_residue"
            grouped_components[key] = {
                "label": f"{resname} ({chain_name}:{resseq}{icode})",
                "selector": selector,
                "resname": resname,
                "chain_id": chain_name,
                "resseq": resseq,
                "icode": icode,
                "component_class": component_class,
                "atom_count": len(atoms),
                "center": center,
                "elements": sorted({atom.element.name.upper() for atom in atoms if atom.element.name}),
                "atom_points": [(atom.pos.x, atom.pos.y, atom.pos.z) for atom in atoms],
                "atom_elements": [str(atom.element.name or "").upper() for atom in atoms],
                "is_structural": False,
                "is_selected": component_class != "water",
                "is_coordinated": False,
            }

    components = list(grouped_components.values())
    ligand_polar_points = [
        point
        for component in components
        if component["component_class"] == "ligand"
        for point, element in zip(component["atom_points"], component["atom_elements"])
        if str(element or "").upper() in {"O", "N", "S"}
    ]
    metal_centers = [component["center"] for component in components if component["component_class"] == "metal"]

    for component in components:
        if component["component_class"] == "water":
            center = component["center"]
            near_ligand = any(_distance(center, target) <= 3.2 for target in ligand_polar_points)
            near_polymer = any(_distance(center, target) <= 3.2 for target in polymer_polar_points)
            near_metal = any(_distance(center, target) <= 2.8 for target in metal_centers)
            is_structural = (near_ligand and near_polymer) or (near_metal and near_polymer)
            component["is_structural"] = is_structural
        elif component["component_class"] == "metal":
            center = component["center"]
            donor_points = [
                point
                for comp in components
                if comp is not component
                for point, element in zip(comp["atom_points"], comp["atom_elements"])
                if str(element or "").upper() in {"O", "N", "S"}
            ] + list(polymer_polar_points)
            component["is_coordinated"] = (
                component["resname"] in _COORDINATION_METAL_CODES
                and sum(1 for point in donor_points if 0.1 < _distance(center, point) <= 2.8) >= 2
            )

    for component in components:
        component.pop("atom_elements", None)
        component.pop("atom_points", None)

    chains = [
        {
            "label": chain_id,
            "polymer_type": payload["polymer_type"],
            "residue_count": len(payload["residues"]),
        }
        for chain_id, payload in sorted(chains_map.items())
    ]
    assemblies = [str(assembly.name or index + 1) for index, assembly in enumerate(structure.assemblies)]
    assembly_chains = {
        str(assembly.name or index + 1): sorted({str(chain) for generator in assembly.generators for chain in generator.chains})
        for index, assembly in enumerate(structure.assemblies)
    }
    if not assemblies:
        assemblies = _detect_bioassemblies(lines)
    return {
        "file_path": str(path),
        "name": path.stem,
        "supported": True,
        "chains": chains,
        "components": components,
        "assemblies": assemblies,
        "assembly_chains": assembly_chains,
        "altloc_values": sorted(altloc_values),
        "missing_residues": _count_missing_remark(lines, "465"),
        "missing_atoms": _count_missing_remark(lines, "470"),
        "atom_count": len(polymer_points) + sum(component["atom_count"] for component in components),
    }


def _scan_pdbqt_fallback(path: Path) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    atoms = [_parse_pdb_like_line(line) for line in lines if line.startswith(("ATOM", "HETATM"))]
    atoms = [atom for atom in atoms if atom is not None]
    polymer_points: list[tuple[float, float, float]] = []
    chains_map: dict[str, dict[str, Any]] = {}
    grouped_components: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    altloc_values = sorted({atom["altloc"] for atom in atoms if atom["altloc"]})
    for atom in atoms:
        if atom["is_polymer"]:
            payload = chains_map.setdefault(
                atom["chain_id"],
                {"label": atom["chain_id"], "polymer_type": _polymer_type_for_resname(atom["resname"]), "residues": set()},
            )
            payload["residues"].add((atom["resname"], atom["resseq"], atom["icode"]))
            polymer_points.append((atom["x"], atom["y"], atom["z"]))
            continue
        key = (atom["resname"], atom["chain_id"], atom["resseq"], atom["icode"])
        payload = grouped_components.setdefault(
            key,
            {
                "label": f"{atom['resname']} ({atom['chain_id']}:{atom['resseq']}{atom['icode']})",
                "selector": f"{atom['resname']}:{atom['chain_id']}:{atom['resseq']}{atom['icode']}",
                "resname": atom["resname"],
                "chain_id": atom["chain_id"],
                "resseq": atom["resseq"],
                "icode": atom["icode"],
                "component_class": _classify_component(atom["resname"], [atom["element"]]),
                "atom_count": 0,
                "center": (0.0, 0.0, 0.0),
                "elements": set(),
                "atom_points": [],
                "is_structural": False,
                "is_selected": True,
                "is_coordinated": False,
            },
        )
        payload["atom_count"] += 1
        payload["elements"].add(atom["element"])
        payload["atom_points"].append((atom["x"], atom["y"], atom["z"]))
    components = []
    for payload in grouped_components.values():
        payload["center"] = _center_from_points(payload["atom_points"])
        payload["elements"] = sorted(payload["elements"])
        payload["is_selected"] = payload["component_class"] != "water"
        components.append(payload)
    ligand_centers = [component["center"] for component in components if component["component_class"] == "ligand"]
    metal_centers = [component["center"] for component in components if component["component_class"] == "metal"]
    for component in components:
        if component["component_class"] == "water":
            center = component["center"]
            component["is_structural"] = any(_distance(center, target) <= 3.5 for target in ligand_centers + metal_centers)
        elif component["component_class"] == "metal":
            center = component["center"]
            points = [point for comp in components for point in comp["atom_points"] if comp is not component]
            component["is_coordinated"] = sum(1 for point in points if 0.1 < _distance(center, point) <= 3.0) >= 2
        component.pop("atom_points", None)
    chains = [
        {"label": chain_id, "polymer_type": payload["polymer_type"], "residue_count": len(payload["residues"])}
        for chain_id, payload in sorted(chains_map.items())
    ]
    return {
        "file_path": str(path),
        "name": path.stem,
        "supported": True,
        "chains": chains,
        "components": components,
        "assemblies": _detect_bioassemblies(lines),
        "altloc_values": altloc_values,
        "missing_residues": _count_missing_remark(lines, "465"),
        "missing_atoms": _count_missing_remark(lines, "470"),
        "atom_count": len(atoms),
    }


def _unsupported_scan(path: Path) -> dict[str, Any]:
    return {
        "file_path": str(path),
        "name": path.stem,
        "supported": False,
        "chains": [],
        "components": [],
        "assemblies": [],
        "altloc_values": [],
        "missing_residues": 0,
        "missing_atoms": 0,
        "atom_count": 0,
    }


def _unsupported_preview(path: Path, options: ReceptorImportOptions) -> dict[str, Any]:
    return {
        "file_path": str(path),
        "name": path.stem,
        "structure": {
            "import_profile": {
                "use_biological_assembly": False,
                "remove_non_structural_waters": bool(options.remove_non_structural_waters),
                "waters_detected": 0,
                "waters_structural": 0,
                "waters_selected": 0,
                "create_binding_sites_from_components": bool(options.create_binding_sites_from_components),
                "remove_cofactors": bool(options.remove_cofactors),
                "remove_altloc": bool(options.remove_altloc),
            },
            "assembly": {"mode": "complete", "selected": "Complete", "available_ids": []},
            "chains": [],
            "components": {
                "ligands": [],
                "metals": [],
                "ions": [],
                "cofactors": [],
                "additives": [],
                "waters": {"count": 0, "structural_count": 0, "selected_count": 0, "selected_residues": []},
            },
            "altloc": {"present": False, "values": [], "selection_policy": "keep_all"},
        },
        "precheck": {
            "missing_residues": {"count": 0, "present": False},
            "missing_atoms": {"count": 0, "present": False},
            "status": "Review",
            "messages": [f"Structural preview is not implemented for {path.suffix or 'this format'}"],
            "valid_for_binding_site": False,
        },
        "workflow": {
            "import_mode": str(options.import_mode or "receptor"),
            "selected_cocrystal_key": str(options.selected_cocrystal_key or ""),
            "activity": str(options.activity_text or "").strip(),
            "ligand_candidates": [],
        },
        "binding_site_specs": [],
        "summary": {
            "name": path.stem,
            "assembly": "Complete",
            "chains": "-",
            "ligands": 0,
            "ligand_labels": [],
            "metals": 0,
            "metal_labels": [],
            "waters": 0,
            "status": "Review",
            "selected_cocrystal_key": "",
        },
    }


def _component_payload(component: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": component["label"],
        "selector": component["selector"],
        "resname": component["resname"],
        "chain_id": component["chain_id"],
        "resseq": component["resseq"],
        "icode": component["icode"],
        "atom_count": component["atom_count"],
        "is_coordinated": bool(component.get("is_coordinated")),
    }


def _is_polymer_residue(resname: str) -> bool:
    return resname in _AMINO_ACIDS or resname in _NUCLEOTIDE_CODES


def _polymer_type_for_resname(resname: str) -> str:
    if resname in _AMINO_ACIDS:
        return "protein"
    if resname in _NUCLEOTIDE_CODES:
        return "nucleotide"
    return "polymer"


def _classify_component(resname: str, elements: list[str]) -> str:
    if resname in _WATER_CODES:
        return "water"
    if resname in _COFACTOR_CODES:
        return "cofactor"
    if resname in _ADDITIVE_CODES:
        return "additive"
    if resname in _ION_CODES:
        return "ion"
    if resname in _COORDINATION_METAL_CODES and len(elements) <= 2:
        return "metal"
    unique_elements = {str(element or "").upper() for element in elements if str(element or "").strip()}
    if len(unique_elements) == 1 and next(iter(unique_elements), "") in _COORDINATION_METAL_CODES:
        return "metal"
    if resname in _ION_CODES:
        return "ion"
    return "ligand"


def _gemmi_center(atoms: list[gemmi.Atom]) -> tuple[float, float, float]:
    points = [(atom.pos.x, atom.pos.y, atom.pos.z) for atom in atoms]
    return _center_from_points(points)


def _center_from_points(points: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    if not points:
        return (0.0, 0.0, 0.0)
    count = float(len(points))
    return (
        sum(point[0] for point in points) / count,
        sum(point[1] for point in points) / count,
        sum(point[2] for point in points) / count,
    )


def _parse_pdb_like_line(line: str) -> dict[str, Any] | None:
    try:
        record_type = line[0:6].strip()
        atom_name = line[12:16].strip()
        altloc = line[16:17].strip()
        resname = line[17:20].strip().upper()
        chain_id = line[21:22].strip() or "-"
        resseq = line[22:26].strip()
        icode = line[26:27].strip()
        x = float((line[30:38] or "0").strip() or 0.0)
        y = float((line[38:46] or "0").strip() or 0.0)
        z = float((line[46:54] or "0").strip() or 0.0)
        element = (line[76:78].strip() or atom_name[:2].strip()).upper()
    except Exception:
        return None
    return {
        "record_type": record_type,
        "atom_name": atom_name,
        "altloc": altloc,
        "resname": resname,
        "chain_id": chain_id,
        "resseq": resseq,
        "icode": icode,
        "x": x,
        "y": y,
        "z": z,
        "element": element,
        "is_polymer": _is_polymer_residue(resname),
    }


def _build_binding_site_specs(
    components: list[dict[str, Any]],
    options: ReceptorImportOptions,
    reference_ligands: list[str] | None = None,
) -> list[dict[str, Any]]:
    if not options.create_binding_sites_from_components:
        return []
    size = (float(options.binding_site_box_size),) * 3
    # Only the kept reference ligands get a binding site (artifact copies were dropped upstream).
    reference_set = None if reference_ligands is None else set(reference_ligands)
    specs: list[dict[str, Any]] = []
    for component in components:
        component_class = component["component_class"]
        if component_class == "ligand":
            if reference_set is not None and str(component.get("selector") or "") not in reference_set:
                continue
            specs.append(
                {
                    "name": f"{component['resname']} site",
                    "source": "ligand",
                    "source_ref": component["selector"],
                    "center": component["center"],
                    "size": size,
                    "extra_data": {"component_class": component_class, "selector": component["selector"]},
                }
            )
        elif component_class == "metal" and component.get("is_coordinated"):
            specs.append(
                {
                    "name": f"{component['resname']} metal site",
                    "source": "metal",
                    "source_ref": component["selector"],
                    "center": component["center"],
                    "size": size,
                    "extra_data": {"component_class": component_class, "selector": component["selector"]},
                }
            )
    return specs


def _build_workflow_payload(components: list[dict[str, Any]], options: ReceptorImportOptions) -> dict[str, Any]:
    ligand_candidates = [component for component in components if component["component_class"] == "ligand"]
    selected_key = str(options.selected_cocrystal_key or "").strip()
    if not selected_key and ligand_candidates and options.import_mode in {"redocking", "rescoring"}:
        selected_key = ligand_candidates[0]["selector"]
    payload = {
        "import_mode": str(options.import_mode or "receptor"),
        "selected_cocrystal_key": selected_key,
        "activity": str(options.activity_text or "").strip(),
        "ligand_candidates": [component["selector"] for component in ligand_candidates],
    }
    if options.remove_cofactors:
        payload["remove_cofactors"] = True
    if options.remove_altloc:
        payload["remove_altloc"] = True
    return payload


def _resolve_kept_chains(all_labels: list[str], selected: list[str]) -> set[str]:
    """Effective chains to keep: the selected subset, or all when nothing (or everything) is picked."""
    if not selected:
        return set(all_labels)
    kept = {label for label in all_labels if label in set(selected)}
    return kept or set(all_labels)


def _selected_chain_ids(scan: dict[str, Any], selected_cocrystal_key: str, explicit: tuple[str, ...] = ()) -> list[str]:
    # An explicit user chain selection wins (proper subset only; all/empty falls through).
    explicit_list = [str(chain_id) for chain_id in (explicit or ())]
    if explicit_list:
        all_labels = [str(chain.get("label")) for chain in list(scan.get("chains") or [])]
        kept = [label for label in all_labels if label in set(explicit_list)]
        if kept and len(kept) < len(all_labels):
            return kept
    selector_parts = _parse_selector(selected_cocrystal_key)
    if selector_parts is not None:
        return [selector_parts[1]]
    chains = list(scan.get("chains") or [])
    if len(chains) == 1:
        return [str(chains[0].get("label") or "-")]
    return []


def _components_to_remove(scan: dict[str, Any], options: ReceptorImportOptions, selected_cocrystal_key: str) -> list[tuple[str, str, str, str]]:
    removable: list[tuple[str, str, str, str]] = []
    for component in list(scan.get("components") or []):
        component_class = str(component.get("component_class") or "")
        selector = str(component.get("selector") or "")
        if selector == selected_cocrystal_key:
            selector_parts = _parse_selector(selector)
            if selector_parts is not None:
                removable.append(selector_parts)
            continue
        if component_class in {"ligand", "additive", "ion"}:
            selector_parts = _parse_selector(selector)
            if selector_parts is not None:
                removable.append(selector_parts)
            continue
        if component_class == "water" and options.remove_non_structural_waters:
            selector_parts = _parse_selector(selector)
            if selector_parts is not None and not bool(component.get("is_structural")):
                removable.append(selector_parts)
            continue
        if component_class == "cofactor" and options.remove_cofactors:
            selector_parts = _parse_selector(selector)
            if selector_parts is not None:
                removable.append(selector_parts)
    return removable


def _parse_selector(selector: str) -> tuple[str, str, str, str] | None:
    text = str(selector or "").strip()
    if text.count(":") < 2:
        return None
    try:
        resname, chain_name, residue = text.split(":", 2)
    except ValueError:
        return None
    residue_text = str(residue or "").strip()
    if not residue_text:
        return None
    resseq = "".join(char for char in residue_text if char.isdigit() or char == "-")
    icode = residue_text[len(resseq):]
    return (str(resname or "").upper(), str(chain_name or "-"), resseq, str(icode or "").strip())


def _build_status(*, atom_count: int, missing_residues: int, missing_atoms: int, altloc_values: list[str]) -> tuple[str, list[str]]:
    messages: list[str] = []
    if atom_count <= 0:
        return "Invalid", ["No structural atoms detected"]
    if missing_residues > 0:
        messages.append(f"Missing residues: {missing_residues}")
    if missing_atoms > 0:
        messages.append(f"Missing atoms: {missing_atoms}")
    if altloc_values:
        messages.append(f"AltLoc detected: {','.join(altloc_values)}")
    if messages:
        return "Review", messages
    return "OK", []


def _count_missing_remark(lines: list[str], remark_code: str) -> int:
    count = 0
    prefix = f"REMARK {remark_code}"
    for line in lines:
        if not line.startswith(prefix):
            continue
        payload = line[len(prefix):].strip()
        if not payload:
            continue
        if payload.startswith(("M RES", "M RES C SSSEQI", "M RES CSSEQI", "M ATOM")):
            continue
        count += 1
    return count


def _detect_bioassemblies(lines: list[str]) -> list[str]:
    ids: list[str] = []
    for line in lines:
        if line.startswith("REMARK 350") and "BIOMOLECULE:" in line:
            payload = line.split("BIOMOLECULE:", 1)[1]
            for item in payload.replace(",", " ").split():
                token = str(item or "").strip()
                if token and token not in ids:
                    ids.append(token)
    return ids


def _distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return sqrt(((a[0] - b[0]) ** 2) + ((a[1] - b[1]) ** 2) + ((a[2] - b[2]) ** 2))


__all__ = [
    "ReceptorImportOptions",
    "build_receptor_import_maps",
    "build_receptor_import_preview",
    "extract_component_to_pdb",
    "preview_receptor_import",
    "scan_receptor_structure",
    "write_processed_receptor",
]
