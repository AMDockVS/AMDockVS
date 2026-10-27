from __future__ import annotations

from amdockvs.docking.engines.programs import VINA_PROGRAM
from amdockvs.core.vocab import ProjectMode
from amdockvs.ui.view_ids import LIGANDS_VIEW_ID, SHARDS_VIEW_ID

class ScopePanel:
    """Molecule-scope resolver for the docking tool.

    There is no scope selector and no scope object: the step acts on the rows its catalog
    table is showing. The user narrows that table (filter a column, or right-click >
    Select) and this reads it back. None = the table narrows nothing, so the step's own
    scope already is the answer.
    """

    def _library_is_sharded(self) -> bool:
        """Does this project's screening library live in shards instead of rows?

        ponytail: asked on every refresh (one COUNT on a small indexed table) instead of
        cached — it flips the moment the first shard import finishes, and a stale answer
        would point the step at the wrong table exactly then.
        """
        try:
            return str(self.runtime.mode) == ProjectMode.HTPVS
        except Exception:  # noqa: BLE001 - no project open yet; the step shows nothing anyway
            return False

    def _ligand_sources(self) -> set[str]:
        """`{"rows", "shards"}` or a part of it: where this experiment reads its ligands.

        Rows are the only answer in `vs`, and for redocking everywhere (it re-docks the
        reference cocrystals, curated rows in both modes). A sharded project reads the Ligands
        step's "Ligands from" boxes, either store or both, on every step: what is prepared
        there is what Preview & Run docks. Without the boxes it screens the library.
        """
        if not self._library_is_sharded() or self._run_kind() == "redocking":
            return {"rows"}
        sources = getattr(self, "ligand_sources", None)
        return {"shards"} if sources is None else sources.sources()

    def _ligand_scope_is_sharded(self) -> bool:
        """Does the experiment leave the ligand rows out (shards only, or nothing picked)?"""
        return "rows" not in self._ligand_sources()

    def _ligand_usage_class(self, run_kind: str) -> str:
        """Docking reads the general rows, redocking the reference ones — whatever else is stored.

        Shards beside the rows don't make the rows reference: only the "As Reference" mark does.
        """
        return "reference" if str(run_kind or "") == "redocking" else "general"

    def _focus_ligand_view(self) -> None:
        """Follow the experiment: switching to redocking swaps Shards for Ligands under you.

        Only while standing on the ligand step — anywhere else the tab the user is reading is
        theirs, not the step's.
        """
        if self.stepper.current_index != self._PREP_STEP["ligand"]:
            return
        opener = getattr(self.window(), "open_or_focus_view", None)
        if callable(opener):
            opener(self._ligand_view_id())

    def _sharded_library_message(self, action: str) -> str:
        """Why this step refuses, in the caller's words ("prepared" / "docked")."""
        return (
            "This project's screening library is sharded on disk, so there are no ligand rows "
            f"here. A sharded library is {action} as a campaign, over whole shards."
        )

    def _ligand_view_id(self) -> str:
        """The catalog table that holds this experiment's ligands."""
        return SHARDS_VIEW_ID if self._ligand_scope_is_sharded() else LIGANDS_VIEW_ID

    @property
    def _selected_ligand_ids(self) -> list[int]:
        # A sharded scope's table lists shards, not molecules: its row ids are not molecule
        # ids and narrowing by them would silently dock the wrong thing.
        if self._ligand_scope_is_sharded():
            return []
        widget = self._catalog_ligand_widget()
        return (widget.scope_ids() if widget is not None else None) or []

    @property
    def _selected_receptor_ids(self) -> list[int]:
        ligand_ids = set(self._selected_ligand_ids) if self._run_kind() == "redocking" else set()
        if ligand_ids:
            # Redocking runs original pairs: picked ligands bring exactly their own receptors
            # ([0] = none of them is in a pair, so no receptor rather than every receptor).
            return sorted({
                int(row.receptor_molecule_id)
                for row in self.runtime.complexes.list(purpose="redocking,reference")
                if int(row.ligand_molecule_id) in ligand_ids
            }) or [0]
        widget = self._catalog_receptor_widget()
        return (widget.scope_ids() if widget is not None else None) or []

    def _ligand_scope(self):
        # Single source of truth for both preparation and docking. Reads the scope here, then
        # hands off to the pure resolver so a worker thread can rebuild the same scope from
        # captured ids without touching widgets.
        return self._resolve_ligand_scope(self._selected_ligand_ids, run_kind=self._run_kind())

    def _resolve_ligand_scope(self, scope_ids: list[int], *, run_kind: str = "docking"):
        # Docking uses general ligands, redocking uses reference ones; the table scope narrows
        # within that, it does not cross it.
        scope = self.runtime.molecules.select(
            role="ligand",
            molecule_kind=self._ligand_type(),
            workflow=VINA_PROGRAM.workflow_key,
            excluded=False,
            usage_class=self._ligand_usage_class(run_kind),
        )
        if scope_ids:
            scope = self.runtime.molecules.filter(scope, filters={"id__in": list(scope_ids)})
        return scope

    # Preparation shares the docking scope (there is only one).
    def _prep_ligand_scope(self):
        return self._ligand_scope()

    def _receptor_scope(self):
        return self.runtime.molecules.select(
            role="receptor",
            molecule_kind=self._receptor_type(),
            workflow=VINA_PROGRAM.workflow_key,
            excluded=False,
        )

    def _resolve_receptor_scope(self, receptor_ids: list[int]):
        scope = self._receptor_scope()
        if receptor_ids:
            scope = self.runtime.molecules.filter(
                scope, filters={"id__in": [int(value) for value in receptor_ids]}
            )
        return scope

    def _selected_receptor_scope(self):
        return self._resolve_receptor_scope(self._selected_receptor_ids)

    def _effective_receptor_ids(self) -> list[int]:
        return self._resolve_receptor_ids(self._selected_receptor_ids)

    def _resolve_receptor_ids(self, scope_ids: list[int]) -> list[int]:
        if scope_ids:
            return sorted({int(value) for value in scope_ids if int(value) > 0})
        return sorted(
            {int(value) for value in self.runtime.molecules.stream_ids(self._receptor_scope()) if int(value) > 0}
        )

    def _rows_for_ids_scoped(self, scope, ids: list[int]) -> list[object]:
        # Pure: caller supplies an already-built scope (no widget access).
        if not ids:
            return []
        narrowed = self.runtime.molecules.filter(scope, filters={"id__in": [int(value) for value in ids]})
        return list(self.runtime.molecules.stream(narrowed))
