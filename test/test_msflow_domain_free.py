"""ms_flow is MolSuite's generic data/orchestration layer: nothing of this app's domain may live in it.

The deny-list sits here, not in ms_flow (a list of chemistry words inside ms_flow would be chemistry
inside ms_flow). It scans the installed package's text: code, comments, docstrings.
"""

import re
from pathlib import Path

import ms_flow

DOMAIN = re.compile(
    r"molecul|ligand|receptor|\bdock(s|ed|ing)?\b|pdbqt|smiles|\bsdf\b|rdkit|meeko|\bvina\b|\bgnina\b"
    r"|autodock|uni-?dock|\bchem|amdock|protein|screening|binding.site|\bposes?\b|librar|qsar"
    r"|enamine|\bzinc\b|\bmol\b|\blig\b|prepared[:_]",
    re.IGNORECASE,
)


def test_ms_flow_holds_no_domain_word():
    root = Path(ms_flow.__file__).parent
    hits = [f"{path.relative_to(root)}:{n}: {line.strip()}"
            for path in sorted(root.rglob("*")) if path.suffix in {".py", ".toml", ".md", ".sql", ".json"}
            for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
            if DOMAIN.search(line)]
    assert not hits, "domain words in ms_flow:\n" + "\n".join(hits)
