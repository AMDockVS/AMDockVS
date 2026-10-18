from __future__ import annotations

from datetime import datetime

from amdockvs.chemistry.structures import mol_from_bytes


def canonical_smiles(mol) -> str:
    """Canonical isomeric SMILES without explicit H atoms: stereo stays (@/@@, / \\) and so does
    the protonation state, since a charged atom carries its Hs in brackets ([NH3+], [O-])."""
    from rdkit import Chem

    try:
        return str(Chem.MolToSmiles(Chem.RemoveHs(mol)) or "")
    except Exception:
        return ""


def inchikey(mol) -> str:
    from rdkit import Chem

    try:
        return str(Chem.MolToInchiKey(mol) or "")
    except Exception:
        return ""


def calculate_basic_descriptors(mol) -> dict[str, float | int]:
    from rdkit import Chem
    from rdkit.Chem import Descriptors

    return {
        "mw": float(Descriptors.MolWt(mol)),
        "logp": float(Descriptors.MolLogP(mol)),
        "hbd": int(Descriptors.NumHDonors(mol)),
        "hba": int(Descriptors.NumHAcceptors(mol)),
        "tpsa": float(Descriptors.TPSA(mol)),
        "rotatable_bonds": int(Descriptors.NumRotatableBonds(mol)),
        "fragment_count": int(len(Chem.GetMolFrags(mol, asMols=False, sanitizeFrags=False))),
        "ring_count": int(Descriptors.RingCount(mol)),
        "aromatic_ring_count": int(Descriptors.NumAromaticRings(mol)),
        "hetero_atom_count": int(Descriptors.NumHeteroatoms(mol)),
        "heavy_atom_count": int(mol.GetNumHeavyAtoms()),
        "formal_charge": int(Chem.GetFormalCharge(mol)),
        "fraction_csp3": float(Descriptors.FractionCSP3(mol)),
    }


_RDKIT2D_CALC = None


def rdkit2d_descriptor_names() -> list[str]:
    """The full RDKit 2D descriptor block (~200 names, whatever this RDKit build exposes)."""
    from rdkit.Chem import Descriptors

    return [name for name, _fn in Descriptors._descList]


def calculate_rdkit2d_descriptors(mol) -> dict[str, float]:
    """All RDKit 2D descriptors for a molecule as {name: value}. Non-finite values (a few
    descriptors return NaN/inf on odd structures) are dropped so the caller/imputer handles them."""
    global _RDKIT2D_CALC
    import math

    from rdkit.ML.Descriptors.MoleculeDescriptors import MolecularDescriptorCalculator

    if _RDKIT2D_CALC is None:
        _RDKIT2D_CALC = MolecularDescriptorCalculator(rdkit2d_descriptor_names())
    values = _RDKIT2D_CALC.CalcDescriptors(mol)
    out: dict[str, float] = {}
    for name, value in zip(_RDKIT2D_CALC.GetDescriptorNames(), values):
        v = float(value)
        if math.isfinite(v):
            out[name] = v
    return out


def calculate_descriptor_rows(items: list[dict], *, fingerprint: dict | None = None) -> list[dict]:
    """Compute physchem descriptors per item. When ``fingerprint`` ({"radius","nbits"}) is given,
    also attach a Morgan fingerprint as ``row["fp_binary"]`` (the bitstring as ascii bytes — a
    later np.frombuffer unpacks it without RDKit) plus fp_type/fp_nbits/fp_radius."""
    fp_radius = int((fingerprint or {}).get("radius", 2))
    fp_nbits = int((fingerprint or {}).get("nbits", 2048))
    results: list[dict] = []
    for item in items:
        molecule_id = int(item.get("molecule_id") or 0)
        row = {
            "molecule_id": molecule_id,
            "mw": None,
            "logp": None,
            "hbd": None,
            "hba": None,
            "tpsa": None,
            "rotatable_bonds": None,
            "fragment_count": None,
            "ring_count": None,
            "aromatic_ring_count": None,
            "hetero_atom_count": None,
            "heavy_atom_count": None,
            "formal_charge": None,
            "fraction_csp3": None,
            "status": "completed",
            "error": "",
            "metadata_json": "{}",
            "created_at": datetime.now(),
        }
        try:
            if not item.get("structure"):
                raise ValueError("The molecule has no structure to describe.")
            mol = mol_from_bytes(item["structure"], item.get("structure_kind"))
            if mol is None:
                raise ValueError(f"Could not parse the structure ({item.get('structure_kind') or 'no format'}).")
            row.update(calculate_basic_descriptors(mol))
            if fingerprint is not None:
                from amdockvs.chemistry.fingerprints import morgan_fingerprint
                from amdockvs.models.descriptors import FingerprintType

                fp = morgan_fingerprint(mol, radius=fp_radius, n_bits=fp_nbits)
                row["fp_binary"] = fp.ToBitString().encode("ascii")
                row["fp_type"] = FingerprintType.ECFP6 if fp_radius >= 3 else FingerprintType.ECFP4
                row["fp_nbits"] = fp_nbits
                row["fp_radius"] = fp_radius
        except Exception as exc:
            row["status"] = "failed"
            row["error"] = str(exc)
        results.append(row)
    return results


__all__ = [
    "calculate_basic_descriptors",
    "calculate_descriptor_rows",
    "calculate_rdkit2d_descriptors",
    "rdkit2d_descriptor_names",
]
