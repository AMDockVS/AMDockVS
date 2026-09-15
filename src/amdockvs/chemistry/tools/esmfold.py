"""ESMFold2 structure prediction through the Biohub REST API. Pure core: no DB, no Qt.

The request builder is generic over chain types (protein / rna / dna / ligand) so co-folding can
reuse it later; AMDock only submits protein chains today. The token is read from `ESM_API_KEY`
and never stored by AMDock.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

import numpy as np

ESM_TOKEN_ENV = "ESM_API_KEY"
FOLD_URL = "https://biohub.ai/api/v1/fold_all_atom"
USAGE_URL = "https://biohub.ai"
FAST_MODEL = "esmfold2-fast-2026-05"
MODELS = (FAST_MODEL, "esmfold2-2026-05")
CHAIN_TYPES = ("protein", "rna", "dna", "ligand")


def chain_input(
    chain_type: str,
    chain_id: str,
    *,
    sequence: str = "",
    smiles: str | None = None,
    ccd: Iterable[str] | None = None,
    modifications: Iterable[tuple[int, str]] | None = None,
) -> dict[str, Any]:
    """One entry of `all_atom_input.sequences`, in the SDK's wire shape."""
    kind = str(chain_type or "").strip().lower()
    if kind not in CHAIN_TYPES:
        raise ValueError(f"Unsupported chain type {chain_type!r}; expected one of {CHAIN_TYPES}.")
    if kind == "ligand":
        ccd_codes = [str(code) for code in ccd or ()]
        if not smiles and not ccd_codes:
            raise ValueError(f"Ligand chain {chain_id} needs a SMILES or CCD codes.")
        return {"smiles": smiles, "id": chain_id, "ccd": ccd_codes or None, "type": "ligand"}
    if not sequence:
        raise ValueError(f"Chain {chain_id} has no sequence.")
    chain: dict[str, Any] = {"sequence": str(sequence), "id": chain_id, "type": kind}
    if modifications:
        chain["modifications"] = [{"position": int(pos), "ccd": str(code)} for pos, code in modifications]
    if kind != "dna":  # the SDK's DNAInput has no MSA field
        chain["msa"] = None
    return chain


def protein_chains(sequence: str) -> list[dict[str, Any]]:
    """`AAA:BBB` (ESMFold's multimer notation) -> protein chains A, B."""
    parts = [part for part in str(sequence or "").strip().upper().split(":") if part]
    if not parts:
        raise ValueError("Empty protein sequence.")
    return [chain_input("protein", _chain_id(index), sequence=part) for index, part in enumerate(parts)]


def _chain_id(index: int) -> str:
    return chr(65 + index) if index < 26 else f"C{index}"


def build_fold_request(chains: Iterable[dict[str, Any]], *, model: str = FAST_MODEL) -> dict[str, Any]:
    if model not in MODELS:
        raise ValueError(f"Unknown ESMFold model {model!r}; expected one of {MODELS}.")
    return {
        "all_atom_input": {"sequences": list(chains)},
        "model": model,
        "include_distogram": False,
        "include_pae": True,
        "include_embeddings": False,
        "num_sampling_steps": 100,
        "num_loops": 20,
        "lm_dropout": 0.3,
        "lm_mask_pct": 0.1 if model == FAST_MODEL else 0.0,
        "msa_max_depth": 1024,
        "msa_column_mask_rate": 0.1,
    }


def api_token() -> str:
    return os.environ.get(ESM_TOKEN_ENV, "").strip()


def fold(body: dict[str, Any], *, token: str | None = None, timeout: float = 900.0) -> dict[str, Any]:
    """POST one fold request. Errors carry a hint: the API answers a bad token with an empty 500."""
    resolved_token = (token or api_token()).strip()
    if not resolved_token:
        raise RuntimeError(f"{ESM_TOKEN_ENV} is not set: paste your Biohub API token first.")
    request = urllib.request.Request(
        FOLD_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {resolved_token}",
            "Content-Type": "application/json",
            "User-Agent": "AMDockVS",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore").strip()[:500]
        raise RuntimeError(
            f"ESMFold API returned HTTP {exc.code}: {detail or 'empty response'}. "
            f"Check that {ESM_TOKEN_ENV} is valid and review your usage at {USAGE_URL}."
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"ESMFold API unreachable: {exc.reason}") from exc
    data = payload.get("data", payload) if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data.get("complex"):
        raise RuntimeError(f"ESMFold API response has no structure. Review your usage at {USAGE_URL}.")
    return data


def complex_to_structure(data: dict[str, Any]):
    """The response's `complex` as a gemmi structure; `pdb` comes back null. pLDDT*100 -> B-factor."""
    import gemmi

    complex_data = data["complex"]
    plddt = np.asarray(data.get("plddt") or [], dtype=float) * 100.0
    lookup = {int(key): str(value) for key, value in dict((complex_data.get("metadata") or {}).get("chain_lookup") or {}).items()}
    names, elements, positions = complex_data["atom_names"], complex_data["atom_elements"], complex_data["atom_positions"]
    chains: dict[str, Any] = {}
    numbering: dict[str, int] = {}
    for token_index, (start, end) in enumerate(complex_data["token_to_atoms"]):
        raw_chain = int(complex_data["chain_id"][token_index])
        chain_id = lookup.get(raw_chain, _chain_id(raw_chain))
        chain = chains.setdefault(chain_id, gemmi.Chain(chain_id))
        numbering[chain_id] = numbering.get(chain_id, 0) + 1
        residue = gemmi.Residue()
        residue.name = str(complex_data["sequence"][token_index])
        residue.seqid = gemmi.SeqId(numbering[chain_id], " ")
        # ponytail: every token is a polymer residue; ligand tokens need NonPolymer once co-folding ships.
        residue.entity_type = gemmi.EntityType.Polymer
        b_factor = float(plddt[token_index]) if token_index < plddt.size else 0.0
        for atom_index in range(int(start), int(end)):
            atom = gemmi.Atom()
            atom.name = str(names[atom_index])
            atom.element = gemmi.Element(str(elements[atom_index]))
            atom.pos = gemmi.Position(*positions[atom_index])
            atom.b_iso = b_factor
            atom.occ = 1.0
            residue.add_atom(atom)
        chain.add_residue(residue)
    model = gemmi.Model("1")
    for chain in chains.values():
        model.add_chain(chain)
    structure = gemmi.Structure()
    structure.add_model(model)
    structure.setup_entities()
    return structure


def prediction_metrics(data: dict[str, Any], *, model: str) -> dict[str, Any]:
    plddt = np.asarray(data.get("plddt") or [], dtype=float)
    return {
        "model": model,
        "plddt_mean": round(float(plddt.mean()) * 100.0, 2) if plddt.size else None,
        "ptm": data.get("ptm"),
        "tokens_used": data.get("tokens_used"),
        "potential_sequence_of_concern": bool(data.get("potential_sequence_of_concern")),
        "warning_messages": list(data.get("warning_messages") or []),
    }


def save_prediction(data: dict[str, Any], structure_path: Path, *, model: str) -> tuple[dict[str, Any], dict[str, str]]:
    """Write the mmCIF (+ pAE sidecar .npy). Returns (metrics, {"pae": absolute path})."""
    structure_path = Path(structure_path)
    structure_path.parent.mkdir(parents=True, exist_ok=True)
    complex_to_structure(data).make_mmcif_document().write_file(str(structure_path))
    files: dict[str, str] = {}
    if data.get("pae") is not None:
        pae_path = structure_path.with_suffix(".pae.npy")
        np.save(pae_path, np.asarray(data["pae"], dtype=np.float32))
        files["pae"] = str(pae_path)
    return prediction_metrics(data, model=model), files


def sequence_from_structure(path: str | Path) -> str:
    """Polymer chains of a structure file as `AAA:BBB`, for forced re-prediction of a 3D protein."""
    import gemmi

    structure = gemmi.read_structure(str(path))
    structure.setup_entities()
    parts = [gemmi.one_letter_code([residue.name for residue in chain.get_polymer()]).upper() for chain in structure[0]]
    return ":".join(part for part in parts if part)


def predict_structure(
    sequence: str,
    structure_path: Path,
    *,
    model: str = FAST_MODEL,
    token: str | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    data = fold(build_fold_request(protein_chains(sequence), model=model), token=token)
    return save_prediction(data, structure_path, model=model)


__all__ = [
    "CHAIN_TYPES",
    "ESM_TOKEN_ENV",
    "FAST_MODEL",
    "MODELS",
    "USAGE_URL",
    "api_token",
    "build_fold_request",
    "chain_input",
    "complex_to_structure",
    "fold",
    "predict_structure",
    "prediction_metrics",
    "protein_chains",
    "save_prediction",
    "sequence_from_structure",
]
