from __future__ import annotations

from amdockvs.docking.programs import VINA_PROGRAM

class ScopePanel:
    """Molecule-scope resolver for the docking tool.

    There is no scope selector and no scope object: the step acts on the rows its catalog
    table is showing. The user narrows that table (filter a column, or right-click >
    Select) and this reads it back. None = the table narrows nothing, so the step's own
    scope already is the answer.
    """

    @property
    def _selected_ligand_ids(self) -> list[int]:
        widget = self._catalog_ligand_widget()
        return (widget.scope_ids() if widget is not None else None) or []

    @property
    def _selected_receptor_ids(self) -> list[int]:
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
            usage_class="reference" if str(run_kind or "") == "redocking" else "general",
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
