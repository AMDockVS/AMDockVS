"""B5: the run preview counts already-docked pairs under the protocol hash the run stores.

The editor hashed the widget config as it was, the run the config as the program validates it
(seed, energy_range, min_rmsd and spacing filled in). The two differed for every program, so the
preview found no pair docked and said "20 of 20 docking(s) to run" when 4 would be skipped."""
import pytest
from pydantic import AliasChoices, BaseModel, Field  # noqa: F401 - preloaded before amdockvs (import cycle)

import amdockvs.ui.catalog  # noqa: F401 - imported first, it is what breaks the ui import cycle
from amdockvs.docking.engines.programs import get_docking_program
from amdockvs.docking.readiness import DockingReadinessService
from amdockvs.docking.planning import DockingProtocol
from amdockvs.docking.rerun import resolved_protocol
from amdockvs.ui.tools.docking.protocol_editor import ProtocolEditorWidget

# What the editor's widgets give for each program at their defaults.
WIDGETS = {
    "vina": {"scoring_function": "vina", "exhaustiveness": 8, "num_modes": 9, "vina_backend": "binary", "vina_cpu": 1},
    "gnina": {"scoring_function": "rescore", "exhaustiveness": 8, "num_modes": 9, "vina_cpu": 1},
    "qvina": {"exhaustiveness": 8}, "adgpu": {"nrun": 20}, "unidock": {"search_mode": "fast"},
}


@pytest.mark.parametrize("program", sorted(WIDGETS))
def test_the_editor_hashes_a_protocol_as_the_run_stores_it(program):
    spec = get_docking_program(program)
    stored = resolved_protocol(spec, spec.validate_config(WIDGETS[program]), None)["hash"]
    assert ProtocolEditorWidget._protocol_hash(program, WIDGETS[program]) == stored


def test_the_preview_takes_out_what_the_run_will_skip():
    """Four receptors, one ligand, five programs; Vina already docked all four under its stored hash."""
    vina = get_docking_program("vina")
    stored = resolved_protocol(vina, vina.validate_config(WIDGETS["vina"]), None)["hash"]

    def count_docked_pairs(*, engine, receptor_ids, protocol_hash):
        return 4 if (engine, protocol_hash) == ("vina", stored) else 0

    service = DockingReadinessService(type("R", (), {"docking": type("D", (), {
        "count_docked_pairs": staticmethod(count_docked_pairs)})()})())
    protocols = [DockingProtocol(program=p, label=p, hash=ProtocolEditorWidget._protocol_hash(p, WIDGETS[p]))
                 for p in sorted(WIDGETS)]
    pairs = service.pairs(ligand_count=1, receptor_ids=[1, 2, 4, 6], protocols=protocols, skip_existing=True)
    assert (pairs.total, pairs.already, pairs.to_run) == (20, 4, 16)
    assert dict(pairs.by_program)["vina"] == 0
