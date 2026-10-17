"""The Build of ligand and receptor rows: each row brings its structure's bytes (`structure`, and
its format in `structure_kind`) and the update hands the result back as a `Payload`, the
molecule's new current structure. What was imported (`stored_payload_id`) is never touched.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ms_flow.payloads import scratch_file

from amdockvs.chemistry.pipeline import LIGAND_STEPS, normalize_steps, run_pipeline
from amdockvs.chemistry.state import mol_has_explicit_hs
from amdockvs.chemistry.structures import mol_from_bytes, sdf_payload
from amdockvs.chemistry.tools import (
    fix_receptor_pdb_file,
    minimize_receptor_openmm_file,
    protonate_receptor_pdb2pqr_file,
    protonate_receptor_reduce_file,
)
from amdockvs.core.constants import SERIES_RECEPTORS
from amdockvs.core.payloads import structure_payload
from amdockvs.core.rows import molecule_model_row
from amdockvs.core.vocab import ModelSource
from amdockvs.io.formats import as_pdb


def decode_metadata(raw_metadata: Any) -> dict[str, Any]:
    if raw_metadata is None:
        return {}
    if isinstance(raw_metadata, dict):
        return dict(raw_metadata)
    if not raw_metadata:
        return {}
    try:
        parsed = json.loads(str(raw_metadata))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def clean_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """extra_data without the keys that described an older structure."""
    merged = decode_metadata(metadata)
    for stale in ("chemistry", "history", "project_root", "state"):
        merged.pop(stale, None)
    return merged


def load_ligand_mol(data: bytes | str | None, kind: str | None):
    mol = mol_from_bytes(data, kind)
    if mol is None:
        raise ValueError(f"Could not parse the ligand structure ({kind or 'no format'}).")
    return mol


def _ligand_state(steps: Sequence[tuple[str, Mapping[str, Any]]], result_mol) -> dict[str, Any]:
    """The flags after the last step that speaks about each one.

    Same values the single-operation branches used to set by hand; folding them makes a
    multi-step run report what it actually produced instead of what its last step alone did.
    """
    is_minimized = False
    for name, step_params in steps:
        if name in {"standardize", "protonate"}:
            is_minimized = False
        elif name == "generate_3d":
            is_minimized = bool(
                result_mol.HasProp("_amdock_is_minimized")
                and result_mol.GetBoolProp("_amdock_is_minimized")
            )
        elif name == "conformers":
            is_minimized = bool(step_params.get("optimize", True))
        elif name == "minimize":
            is_minimized = True
    return {
        # Read off the molecule: whichever step added the Hs (protonate, 3D, minimize), they are there.
        "has_hs": mol_has_explicit_hs(result_mol),
        "has_3d": result_mol.GetNumConformers() > 0,
        "is_minimized": is_minimized,
    }


def transform_ligand_rows(
    *,
    operations: str | Sequence[Any],
    rows: Iterable[Mapping[str, Any]],
    params: Mapping[str, Any] | None = None,
    next_model_index_by_entity: Mapping[int, int] | None = None,
    progress_cb=None,
) -> dict[str, Any]:
    """Apply one or more chemistry steps to a batch of ligand rows.

    `operations` is a step list — `[("standardize", {}), ("protonate", {"ph": 7.4})]` — or a
    bare name for a single step. The batch goes through `run_pipeline` once and only the final
    molecule is kept, so standardize+protonate+3D gives one payload per ligand, not three. A
    model row and the molecule it makes current share one `Payload` object.
    """
    resolved_steps = normalize_steps(operations)
    if not resolved_steps:
        raise ValueError("No ligand chemistry operation was given.")
    for name, _step_params in resolved_steps:
        if name not in LIGAND_STEPS:
            raise ValueError(f"Unsupported ligand chemistry operation: {name}")

    normalized_params = dict(params or {})
    next_index_map = {int(key): int(value) for key, value in dict(next_model_index_by_entity or {}).items()}
    row_list = [dict(row) for row in rows]
    updates: list[dict[str, Any]] = []
    failed_rows: list[dict[str, Any]] = []
    # ponytail: one shared bag behind each step's own params, and every step takes only the
    # keys its core declares. Two steps that name a parameter alike (`fragment_mode`) share the
    # bag's value — pass it per step to tell them apart.
    resolved_steps = [
        (name, {**normalized_params, **dict(step_params)}) for name, step_params in resolved_steps
    ]
    last_name = resolved_steps[-1][0]
    # A minimize may follow the ensemble; the ensemble is still what gets written.
    writes_ensemble = any(name == "conformers" for name, _ in resolved_steps)

    # Load the whole batch first: protonation runs once over the set, so the pipeline needs
    # every molecule in hand before the first step. A slot that fails to load carries its
    # exception through the pipeline and lands in `failed_rows` below, still in position.
    batch: list[Any] = []
    for index, row in enumerate(row_list, start=1):
        loaded = None  # a `None` slot is not a failure: there is nothing to write back to
        if int(row.get("id") or 0) > 0:
            try:
                loaded = load_ligand_mol(row.get("structure"), row.get("structure_kind"))
            except Exception as exc:
                loaded = exc
        # One append each, always: the write-back loop pairs this list with `row_list`.
        batch.append(loaded)
        if progress_cb is not None:
            progress_cb((index / max(1, len(row_list))) * 50.0)

    results = run_pipeline(batch, resolved_steps)

    for index, (row, result) in enumerate(zip(row_list, results), start=1):
        ligand_id = int(row.get("id") or 0)
        if progress_cb is not None:
            progress_cb(50.0 + (index / max(1, len(row_list))) * 50.0)
        if ligand_id <= 0:
            continue
        if result is None or isinstance(result, Exception):
            failed_rows.append({
                "entity_id": ligand_id,
                "error": str(result) if result is not None else "No structure was produced.",
            })
            continue
        try:
            model_rows: list[dict[str, Any]] = []
            current_model_index = row.get("current_model_index")
            next_index = int(next_index_map.get(ligand_id, 0))

            if writes_ensemble:
                current = None
                for offset, conformer in enumerate(result.GetConformers()):
                    payload = sdf_payload(result, conf_id=conformer.GetId())
                    if current is None:
                        current, current_model_index = payload, next_index + offset
                    model_rows.append(molecule_model_row(
                        molecule_id=ligand_id, model_index=next_index + offset, payload_id=payload,
                        source=ModelSource.ETKDG,
                    ))
            else:
                current = sdf_payload(result)
                if last_name in {"generate_3d", "minimize"}:
                    current_model_index = next_index
                    model_rows.append(molecule_model_row(
                        molecule_id=ligand_id, model_index=next_index, payload_id=current,
                        source=ModelSource.RDKIT,
                    ))
                elif result.GetNumConformers() == 0:
                    # standardize / protonate keep whatever model was current, unless the
                    # result has no coordinates at all.
                    current_model_index = None

            state = {
                **_ligand_state(resolved_steps, result),
                "conformer_count": result.GetNumConformers(),
            }
            update = {
                "entity_id": ligand_id,
                "extra_data": clean_metadata(row.get("extra_data")),
                "current_model_index": None if current_model_index is None else int(current_model_index),
                "state": state,
                "model_rows": model_rows,
            }
            if current is not None:
                update["current_payload_id"] = current
            updates.append(update)
        except Exception as exc:
            failed_rows.append({"entity_id": ligand_id, "error": str(exc)})
    return {
        "updates": updates,
        "failure_count": len(failed_rows),
        "failure_samples": failed_rows[:10],
        "processed_count": len(row_list),
        "updated_count": len(updates),
    }


def run_receptor_tool(data: bytes, kind: str, run: Callable[[Path, Path], Any]) -> bytes:
    """`run(source_pdb, output_pdb)` over stored bytes; the PDB it writes comes back as bytes.

    pdbfixer, reduce, pdb2pqr and OpenMM all read PDB; the stored structure is mmCIF. Every file
    (the input, a converted copy, the output and pdb2pqr's .pqr beside it) is gone on return.
    """
    with (
        scratch_file(data, f".{str(kind or 'pdb').lstrip('.').lower()}") as source,
        as_pdb(source) as readable,
        tempfile.TemporaryDirectory(prefix="amdockvs-receptor-") as work,
    ):
        output = Path(work) / "receptor.pdb"
        run(readable, output)
        return output.read_bytes()


def transform_receptor_rows(
    *,
    operation: str,
    rows: Iterable[Mapping[str, Any]],
    params: Mapping[str, Any] | None = None,
    next_model_index_by_entity: Mapping[int, int] | None = None,
    progress_cb=None,
) -> list[dict[str, Any]]:
    operation_name = str(operation or "").strip().lower()
    if operation_name not in {"fix", "protonate", "minimize"}:
        raise ValueError(f"Unsupported receptor chemistry operation: {operation}")

    normalized_params = dict(params or {})
    next_index_map = {int(key): int(value) for key, value in dict(next_model_index_by_entity or {}).items()}
    row_list = [dict(row) for row in rows]
    updates: list[dict[str, Any]] = []
    for index, row in enumerate(row_list, start=1):
        receptor_id = int(row.get("id") or 0)
        if receptor_id <= 0:
            continue
        if not row.get("structure"):
            raise ValueError(f"Receptor {receptor_id} has no structure.")
        current_model_index = 0 if not bool(row.get("has_3d")) and row.get("current_model_index") is None else int(next_index_map.get(receptor_id, 0))
        if operation_name == "fix":
            def run(source: Path, output: Path) -> None:
                fix_receptor_pdb_file(
                    source_path=source,
                    output_path=output,
                    add_missing_residues=bool(normalized_params.get("add_missing_residues", True)),
                    add_missing_atoms=bool(normalized_params.get("add_missing_atoms", True)),
                    replace_nonstandard=bool(normalized_params.get("replace_nonstandard", True)),
                    remove_heterogens=bool(normalized_params.get("remove_heterogens", False)),
                    keep_water=bool(normalized_params.get("keep_water", True)),
                )
            state = {"has_hs": False, "has_3d": True, "is_minimized": False, "fixed_with": "pdbfixer"}
        elif operation_name == "protonate":
            method = str(normalized_params.get("method") or "reduce").strip().lower()
            if method == "reduce":
                def run(source: Path, output: Path) -> None:
                    protonate_receptor_reduce_file(source_path=source, output_path=output)
            elif method == "pdb2pqr":
                def run(source: Path, output: Path) -> None:
                    # pdb2pqr too gives the PDB: it writes the PQR as a sidecar, which is dropped
                    protonate_receptor_pdb2pqr_file(
                        source_path=source,
                        output_path=output,
                        forcefield=str(normalized_params.get("forcefield", "AMBER")),
                        ph=float(normalized_params.get("ph", 7.0)),
                    )
            else:
                raise ValueError("protonate_receptors method must be 'reduce' or 'pdb2pqr'.")
            state = {"has_hs": True, "has_3d": True, "is_minimized": False, "protonation_model": method}
        else:
            forcefields = tuple(normalized_params.get("forcefields") or ("amber14-all.xml",))

            def run(source: Path, output: Path) -> None:
                minimize_receptor_openmm_file(
                    source_path=source,
                    output_path=output,
                    forcefields=forcefields,
                    max_iterations=int(normalized_params.get("max_iterations", 500)),
                    tolerance_kj_mol=float(normalized_params.get("tolerance_kj_mol", 10.0)),
                )
            state = {"has_hs": True, "has_3d": True, "is_minimized": True, "forcefield": list(forcefields)}

        current = structure_payload(
            run_receptor_tool(bytes(row["structure"]), str(row.get("structure_kind") or ""), run),
            "pdb", SERIES_RECEPTORS,
        )
        updates.append(
            {
                "entity_id": receptor_id,
                "extra_data": clean_metadata(row.get("extra_data")),
                "current_payload_id": current,
                "current_model_index": current_model_index,
                "state": state,
                "model_rows": [
                    molecule_model_row(
                        molecule_id=receptor_id,
                        model_index=current_model_index,
                        payload_id=current,
                        source={"fix": ModelSource.FIXED, "protonate": ModelSource.PROTONATED}.get(operation_name, ModelSource.MINIMIZED),
                    )
                ],
            }
        )
        if progress_cb is not None:
            progress_cb((index / max(1, len(row_list))) * 100.0)
    return updates


__all__ = [
    "clean_metadata",
    "decode_metadata",
    "load_ligand_mol",
    "run_receptor_tool",
    "transform_ligand_rows",
    "transform_receptor_rows",
]
