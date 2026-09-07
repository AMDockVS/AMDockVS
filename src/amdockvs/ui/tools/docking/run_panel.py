from __future__ import annotations

from uuid import uuid4

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget, QGridLayout,
)

from amdockvs.core.constants import DEFAULT_LOCAL_CPU_EXECUTOR
from amdockvs.docking.planning import protocol_job_key
from amdockvs.docking.shard_jobs import (
    HIT_MODE_THRESHOLD,
    HIT_MODE_TOP_N,
    HIT_SAFETY_CAP,
    RANKED_PREFILTER_SCORE,
)
from amdockvs.docking.engines.programs import get_docking_program
from amdockvs.ui.common.async_query import run_async
from amdockvs.ui.common.widgets import split_button
from amdockvs.core.vocab import MoleculeType

def _spinbox(*, minimum: int, maximum: int, value: int) -> QSpinBox:
    widget = QSpinBox()
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    return widget


from amdockvs.docking.planning import (
    DockingProtocol,
    DockingRunIdentity,
    DockingRunRequest,
    docking_signature,
)


class RunPanel:
    """Readiness summary, validation and docking launch component."""

    def _build_preview_run_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        # Plain QGroupBox: pyqtgraph's collapsible one used to live here, and its setCollapsed
        # runs setVisible over EVERY child — which re-showed the busy overlay for good.
        self.req_box = QGroupBox("Run Scope", page)

        run_scope_layout = QGridLayout(self.req_box)

        # No scope selector: the run covers whatever the catalog tables are scoped to.
        run_scope_layout.addWidget(QLabel("Ligands:", page), 0, 0)
        self.req_ligands_count = QLabel("—", page)  # prepared / total
        run_scope_layout.addWidget(self.req_ligands_count, 0, 1, 1, 2)

        run_scope_layout.addWidget(QLabel("Receptors:", page), 1, 0)
        self.req_receptors_count = QLabel("—", page)  # prepared / total
        run_scope_layout.addWidget(self.req_receptors_count, 1, 1, 1, 2)

        # What the run actually costs: one docking per (ligand, receptor, protocol).
        run_scope_layout.addWidget(QLabel("Dockings:", page), 2, 0)
        self.req_pairs_count = QLabel("—", page)  # to run / total pairs
        self.req_pairs_count.setToolTip(
            "Ligands x receptors x selected programs. With 'Skip pairs already docked' on, pairs "
            "that already have a result for the same protocol are not run again."
        )
        run_scope_layout.addWidget(self.req_pairs_count, 2, 1, 1, 2)

        self.check_status_label = QLabel("Open this step to check.", page)
        self.check_status_label.setWordWrap(True)
        run_scope_layout.addWidget(self.check_status_label, 3, 0, 1, 3)

        layout.addWidget(self.req_box)

        # Sharded library only: a campaign writes back only what passes the gate. Without a
        # threshold and a cap it would materialize the whole library as molecule rows, which is
        # the failure mode the sharded mode exists to avoid — so both are required, not optional.
        self.hits_box = QGroupBox("Screening hits", page)
        hits_layout = QFormLayout(self.hits_box)
        self.hit_threshold = QDoubleSpinBox(self.hits_box)
        self.hit_threshold.setRange(-100.0, 0.0)
        self.hit_threshold.setDecimals(1)
        self.hit_threshold.setSingleStep(0.5)
        self.hit_threshold.setValue(-8.0)
        self.hit_cap = _spinbox(minimum=1, maximum=1000000, value=1000)
        self.hit_mode_combo = QComboBox(self.hits_box)
        self.hit_mode_combo.addItem("Best N of the whole run (ranked, written at the end)", HIT_MODE_TOP_N)
        self.hit_mode_combo.addItem("First hits past the threshold (stops early)", HIT_MODE_THRESHOLD)
        self.hit_mode_combo.setToolTip(
            "Ranked keeps the best N of everything scored, but the run cannot stop early and no "
            "result is visible until it finishes. Threshold writes hits as they come back."
        )
        # The same number means two things, so the label says which one: a stop condition in
        # streaming mode, the size of the ranking in ranked mode.
        self.hit_cap_label = QLabel("", self.hits_box)
        self.hit_threshold_label = QLabel("Keep at or below (kcal/mol)", self.hits_box)
        hits_layout.addRow(self.hit_threshold_label, self.hit_threshold)
        hits_layout.addRow(self.hit_cap_label, self.hit_cap)
        hits_layout.addRow("Selection", self.hit_mode_combo)
        self.offtarget_reference_combo = QComboBox(self.hits_box)
        self.offtarget_reference_combo.addItem("None — dock the full library against every receptor", None)
        self.offtarget_reference_combo.setToolTip(
            "With a reference, dock the full library against it first, then dock only its "
            "Top-N ligands against the other receptors. Available when at least two receptors "
            "are ready and ranked Top-N selection is active."
        )
        self.offtarget_reference_combo.currentIndexChanged.connect(self._sync_hit_cap_label)
        self.offtarget_reference_combo.currentIndexChanged.connect(lambda _index: self._check_requirements())
        hits_layout.addRow("Off-target docking reference", self.offtarget_reference_combo)
        self._ready_receptor_options: list[tuple[int, str]] = []
        self.hit_mode_combo.currentIndexChanged.connect(self._sync_hit_cap_label)
        self._sync_hit_cap_label()
        self.hits_box.setToolTip(
            "Only poses scoring at or below the threshold become molecules and results; the rest "
            "are scored and discarded. The cap ends the campaign once that many hits are in."
        )
        self.hits_box.setVisible(False)
        layout.addWidget(self.hits_box)
        # Run resources — program-specific settings (CPU per task, exhaustiveness…) live on
        # the Programs step, not here.
        run_box = QGroupBox("Run resources", page)
        run_layout = QFormLayout(run_box)
        # One docking pair per chunk by default so independent, long-running dockings
        # spread across the executor pool instead of serializing inside one chunk.
        self.batch_size = _spinbox(minimum=1, maximum=1024, value=1)
        self.skip_existing_check = QCheckBox("Skip pairs already docked", run_box)
        self.skip_existing_check.setChecked(True)
        self.skip_existing_check.setToolTip(
            "Before running, drop receptor–ligand pairs that already have results for this "
            "engine (a single indexed scan). Uncheck to re-dock and replace previous results."
        )
        self.batch_size_label = QLabel("Batch Size", run_box)
        run_layout.addRow(self.batch_size_label, self.batch_size)
        run_layout.addRow(self.skip_existing_check)
        self.compute_interactions_check = QCheckBox("Compute interactions after docking", run_box)
        self.compute_interactions_check.setToolTip(
            "Submit an async interaction job chained after this docking run. Useful for small "
            "sets or remote/HPC execution; for large VS, compute interactions later from Results."
        )
        run_layout.addRow(self.compute_interactions_check)
        self.render_diagrams_check = QCheckBox("Render 2D interaction diagrams", run_box)
        self.render_diagrams_check.setToolTip(
            "Draw each pose's 2D interaction diagram inside the docking run itself (one render "
            "subprocess per pose — cheap for a handful of poses, expensive for a large VS). "
            "Leave it off and render on demand from Results instead."
        )
        run_layout.addRow(self.render_diagrams_check)
        self.run_button = split_button("Run Docking", run_box, on_click=self._run_docking)
        self.run_button.menu().addAction(
            "Save to workflow…", self._add_docking_to_workflow
        ).setToolTip(
            "Queue this docking (current scope and settings) as a step in the active workflow "
            "instead of running it now."
        )
        run_layout.addRow(self.run_button)
        self.open_results_button = QPushButton("Open Results", run_box)
        self.open_results_button.clicked.connect(
            lambda: self.window().central_widget.open_or_focus_view("workspace.complexes")
        )
        run_layout.addRow(self.open_results_button)
        layout.addWidget(run_box)
        layout.addStretch(1)
        self._on_run_kind_changed()
        return page

    def _refresh_requirement_preview(self) -> None:
        # Debounced entry point — just (re)dispatches the off-thread aggregate.
        self._dispatch_refresh()

    def _dispatch_refresh(self, *, force: bool = False) -> None:
        # Single-flight: coalesce while a worker is running so the threadpool never floods.
        if self._refresh_inflight:
            self._refresh_pending = True
            self._refresh_pending_force = self._refresh_pending_force or force
            return
        # Capture ALL widget state here (GUI thread), then do every DB read in a worker so
        # entering the step / changing selection never blocks the UI on large molecule sets.
        step = self.stepper.current_index
        inputs = {
            "step": step,
            "program": self._program(),
            "run_kind": self._run_kind(),
            "receptor_type": self._receptor_type(),
            "ligand_type": self._ligand_type(),
            "sel_lig": self._selected_ligand_ids,
            "sel_rec": self._selected_receptor_ids,
            "focused": self._focused_receptor_id,
            "prep_engines": self._selected_prep_engines(),
            "programs_chosen": bool(self._selected_protocols()),
        }
        sig = (
            step, inputs["program"], inputs["run_kind"], inputs["receptor_type"], inputs["ligand_type"],
            tuple(inputs["sel_lig"]), tuple(inputs["sel_rec"]),
            inputs["focused"], inputs["programs_chosen"], tuple(inputs["prep_engines"]),
        )
        if not force and sig == self._last_refresh_sig:
            return
        self._refresh_inflight = True
        self._refresh_token += 1
        token = self._refresh_token
        # The scope value is what visibly lags on huge libraries — a small inline spinner on it
        # (not a whole-panel overlay) is the right weight for a single label.
        busy_label = {1: self.ligand_scope_label, 2: self.receptor_scope_label}.get(step)
        run_async(
            lambda: self._compute_refresh(inputs),
            lambda data: self._on_refresh_done(data, token, sig),
            on_error=lambda _exc: self._on_refresh_done(None, token, sig),
            busy=busy_label,
            compact=True,
        )

    def _on_refresh_done(self, data: dict | None, token: int, sig) -> None:
        self._refresh_inflight = False
        if data is not None:
            self._last_refresh_sig = sig
            self._apply_refresh(data, token)
        if self._refresh_pending:
            self._refresh_pending = False
            force, self._refresh_pending_force = self._refresh_pending_force, False
            self._dispatch_refresh(force=force)

    def _compute_refresh(self, inp: dict) -> dict:
        # Worker thread: only DB reads + pure helpers, never widget access. Gated by the
        # CURRENT step so we don't run the heavy count/check_required scans for panels the
        # user isn't even looking at. molecules.count() materializes the set, so each call is
        # O(n) — only pay for what the visible step shows.
        step = inp["step"]
        data: dict = {"step": step, "programs_chosen": inp["programs_chosen"]}
        if step == 0:  # Programs — nothing in the DB to show here.
            return data

        if step == 1:  # Ligands — just the scope label.
            if inp.get("run_kind") != "redocking" and self._library_is_sharded():
                # No molecule rows to count: the library is on disk, so count shards instead.
                counts = self.runtime.molecules.shard_counts()
                data["sharded"] = True
                data["shards_total"] = counts["shards"]
                data["shard_records"] = counts["records"]
                data["shards_prepared"] = counts["prepared"]
                return data
            lig_scope = self._resolve_ligand_scope(
                inp["sel_lig"], run_kind=inp.get("run_kind", "docking")
            )
            data["ligands_total"] = self.runtime.molecules.count(lig_scope)
            # One count per preparation family (K is the number of distinct engines, today 1):
            # each family reconciles against itself, so no cross-engine aggregate is needed.
            by_engine = {
                engine: self.runtime.molecules.count(
                    self.runtime.molecules.filter(
                        lig_scope, filters={"prepared": True, "prepared_engine_key": engine}
                    )
                )
                for engine in inp["prep_engines"]
            }
            data["ligands_prepared_by_engine"] = by_engine
            # For the step badge only: a molecule is "done" when every selected family has it,
            # and min() is that count exactly when the prepared sets nest (the normal case).
            data["ligands_prepared"] = min(by_engine.values()) if by_engine else 0
            data["ligands_failed"] = sum(
                self._count_failed_preparations(lig_scope, role_type="ligand", engine=engine)
                for engine in inp["prep_engines"]
            )
            return data

        if step == 3:  # Preview & Run — NO automatic scan at all; verification is explicit.
            return data

        # Receptors (2): cards + receptor scope label + receptor-only prep/grid status. Receptor
        # sets are small, so no ligand scan and no full requirement check here.
        self._fill_receptor_data(inp, data)
        data["preview"] = self._compute_receptor_preview(inp, self._resolve_receptor_ids(inp["sel_rec"]))
        return data

    def _fill_receptor_data(self, inp: dict, data: dict) -> None:
        # receptors_total is used only as a presence/gate signal + scope label; prep/grid
        # readiness comes from the check_receptors preview, so no extra prepared-count query here.
        data["receptors_total"] = self.runtime.molecules.count(self._receptor_scope())
        rec_label_scope = self._resolve_receptor_scope(inp["sel_rec"])
        data["rec_scope_total"] = self.runtime.molecules.count(rec_label_scope)
        program = get_docking_program(inp["program"])
        if program.requires_receptor_preparation:
            prep_engine = str(program.preparation_engine)
            data["rec_scope_prepared"] = self.runtime.molecules.count(
                self.runtime.molecules.filter(
                    rec_label_scope, filters={"prepared": True, "prepared_engine_key": prep_engine}
                )
            )
            data["rec_scope_failed"] = self._count_failed_preparations(
                rec_label_scope, role_type="receptor", engine=prep_engine
            )
        else:
            data["rec_scope_prepared"] = data["rec_scope_total"]
            data["rec_scope_failed"] = 0

    def _compute_receptor_preview(self, inp: dict, receptor_ids: list[int]) -> dict:
        if not receptor_ids:
            focused = int(inp.get("focused") or 0)
            return {
                "kind": "no_receptors",
                "focused_has_grid": bool(focused) and self._focused_has_grid(inp, [focused]),
            }
        try:
            status = self.runtime.docking.check_receptors(
                program=inp["program"],
                receptor_set=self._resolve_receptor_scope(receptor_ids),
            )
        except Exception as exc:
            return {"kind": "error", "message": str(exc)}
        counts = dict(status.get("counts") or {})
        rec_ready = int(counts.get("receptors_ready") or 0)
        rec_total = int(counts.get("receptors_total") or 0)
        grid_ready = int(counts.get("receptor_grids_ready") or 0)
        return {
            "kind": "receptor_only",
            "rec_ready": rec_ready,
            "rec_total": rec_total,
            "grid_ready": grid_ready,
            "focused_has_grid": self._focused_has_grid(inp, receptor_ids),
        }

    def _focused_has_grid(self, inp: dict, receptor_ids: list[int]) -> bool:
        """Does the focused receptor have an active-site grid? (gates the flex-residues panel)

        The grid itself — which BS, center, size — is displayed by the PyMOL Grid Box panel,
        so only the yes/no is needed here.
        """
        focused_id = inp["focused"] if inp["focused"] in receptor_ids else receptor_ids[0]
        if not self._rows_for_ids_scoped(self._receptor_scope(), [focused_id]):
            return False
        program = get_docking_program(inp["program"])
        if not program.requires_binding_site:
            return False
        grid = self.runtime.docking.get_grid(
            receptor_id=int(focused_id), engine=program.preparation_engine
        )
        return bool(grid) and len(grid.get("center") or []) == 3 and len(grid.get("size") or []) == 3

    # -- flexible residues -------------------------------------------------
    def _apply_refresh(self, data: dict, token: int) -> None:
        # GUI thread: drop stale results so the freshest dispatch wins. Only the keys the
        # current step computed are present, so apply defensively.
        if token != self._refresh_token:
            return
        self.step_programs.set_done(bool(data.get("programs_chosen")))
        if data.get("sharded"):
            total = int(data.get("shards_total") or 0)
            prepared = int(data.get("shards_prepared") or 0)
            self.ligand_scope_label.setText(
                f"{total} shard(s) · {data['shard_records']} molecule(s) — sharded library, "
                f"{prepared} shard(s) prepared. Preparation runs over whole shards."
            )
            # Per-engine family counts are a molecule-row idea: a shard is prepared for the
            # engine that rewrote it, and its row carries no engine breakdown.
            self._set_prep_family_counts({}, 0)
            self.prepare_ligands_button.setEnabled(True)
            self._mark_step(self.step_ligands, prepared, total)
            return
        self.prepare_ligands_button.setEnabled(True)
        if "ligands_prepared" in data:
            ligand_failed = int(data.get("ligands_failed") or 0)
            failed_text = f" · {ligand_failed} failed" if ligand_failed else ""
            # Scope = which ligands (one, shared). "N prepared" is per family, so it lives on
            # the family rows below, not here — a single number would be one family's count
            # presented as the total.
            self.ligand_scope_label.setText(f"{data['ligands_total']} ligand(s) in scope{failed_text}")
            self._set_prep_family_counts(
                data.get("ligands_prepared_by_engine") or {}, int(data["ligands_total"])
            )
            # Green only when ALL in scope are prepared; orange (⚠) for a partial subset;
            # red when none are prepared. Surfaces once you leave the step.
            self._mark_step(self.step_ligands, data["ligands_prepared"], data["ligands_total"])
        if "receptors_total" not in data:
            return
        receptor_failed = int(data.get("rec_scope_failed") or 0)
        failed_text = f" · {receptor_failed} failed" if receptor_failed else ""
        scope_text = (
            f"{data['rec_scope_total']} receptor(s) in scope · "
            f"{data['rec_scope_prepared']} prepared{failed_text}"
        )
        self.receptor_scope_label.setText(scope_text)

        p = data["preview"]
        if p["kind"] == "no_receptors":
            focused_has_grid = bool(p.get("focused_has_grid"))
            self.flex_box.setEnabled(focused_has_grid)
            self._sync_flex_for_focus(focused_has_grid)
            self.step_receptors.set_error(True)
            return
        if p["kind"] == "error":
            self.flex_box.setEnabled(False)
            return
        rec_ready, rec_total, grid_ready = p["rec_ready"], p["rec_total"], p["grid_ready"]
        failed_text = f" / failed {receptor_failed}" if receptor_failed else ""

        # Grid coverage rides on the scope line — the only Active Site number the PyMOL Grid
        # Box panel doesn't already show.
        self.receptor_scope_label.setText(
            f"{scope_text} · {grid_ready} with grid"
            + (f" · {max(0, rec_total - grid_ready)} missing" if grid_ready < rec_total else "")
        )
        # A receptor is ready when it's both prepared AND has a grid; mark on the weakest link.
        self._mark_step(self.step_receptors, min(rec_ready, grid_ready), rec_total)
        # Flexible residues need an active site on the focused receptor.
        focused_has_grid = bool(p.get("focused_has_grid"))
        self.flex_box.setEnabled(focused_has_grid)
        self._sync_flex_for_focus(focused_has_grid)

    @staticmethod
    def _mark_step(step, prepared: int, total: int) -> None:
        if total > 0 and prepared >= total:
            step.set_done(True)
        elif prepared > 0:
            step.set_warning(True)
        else:
            step.set_error(True)

    @staticmethod
    def _short(value: object) -> str:
        if isinstance(value, dict):
            return ", ".join(f"{k}={RunPanel._short(v)}" for k, v in value.items()) or "—"
        if isinstance(value, (list, tuple)):
            return f"{len(value)} item(s)"
        text = str(value)
        # Job ids are long hex; show a recognizable prefix only.
        return text[:8] + "…" if len(text) > 12 else text

    def _append_status(self, title: str, payload) -> None:
        # The status strip is currently disabled; keep this a safe no-op if it's absent.
        label = getattr(self, "status_label", None)
        if label is None:
            return
        body = payload if isinstance(payload, str) else self._short(payload)
        label.setText(f"{title} — {body}")

    def _requirement_inputs(self) -> dict:
        # Capture the run scope (not the per-step mode) so the check matches the run and never
        # collapses to an empty "selected" scope after preparation cleared the table selection.
        return {
            "sharded": self._ligand_scope_is_sharded(),
            "program": self._program(),
            "run_kind": self._run_kind(),
            "receptor_type": self._receptor_type(),
            "ligand_type": self._ligand_type(),
            "sel_lig": self._selected_ligand_ids,
            "sel_rec": self._selected_receptor_ids,
            # For the pair count: one docking per (ligand, receptor, protocol).
            "protocols": [(str(p.get("program") or ""), str(p.get("hash") or "")) for p in self._selected_protocols()],
            "skip_existing": bool(self.skip_existing_check.isChecked()),
            "offtarget_reference_id": (
                int(self.offtarget_reference_combo.currentData())
                if self.offtarget_reference_combo.currentData() is not None
                else None
            ),
        }

    def _compute_redocking_requirement_counts(self, inp: dict) -> dict:
        return self.readiness_service.evaluate_redocking(
            program=inp["program"],
            ligand_type=inp.get("ligand_type") or MoleculeType.SMALL_MOLECULE,
            receptor_type=inp.get("receptor_type") or MoleculeType.PROTEIN,
        ).as_mapping()

    def _compute_requirement_counts(self, inp: dict) -> dict:
        if inp.get("sharded"):
            # No molecule rows to count: the unit is the shard. Receptor readiness is the same
            # question as always, so it goes through the same service.
            counts = self.runtime.molecules.shard_counts()
            receptors = self.readiness_service.receptors(
                self._resolve_receptor_scope(inp["sel_rec"]), program=inp["program"]
            )
            ready_receptors = []
            for receptor_id in receptors.ready_ids:
                getter = getattr(self.runtime.molecules, "get", None)
                receptor = getter(int(receptor_id)) if callable(getter) else None
                ready_receptors.append((int(receptor_id), "" if receptor is None else str(receptor.name)))
            return {
                "mode": "sharded",
                "ligands": {"total": counts["shards"], "ready": counts["prepared"], "failed": 0},
                "receptors": receptors.as_mapping(),
                "records": counts["prepared_records"],
                "ready_receptor_ids": list(receptors.ready_ids),
                "ready_receptors": ready_receptors,
                "offtarget_reference_id": inp.get("offtarget_reference_id"),
                "ready": bool(counts["prepared"] and receptors.ready_ids),
            }
        if inp.get("run_kind") == "redocking":
            return self._compute_redocking_requirement_counts(inp)
        ligand_scope = self._resolve_ligand_scope(inp["sel_lig"], run_kind=inp.get("run_kind", "docking"))
        receptor_scope = self._resolve_receptor_scope(inp["sel_rec"])
        protocols = tuple(
            DockingProtocol(program=str(program), label=str(program), hash=str(hash_value))
            for program, hash_value in (inp.get("protocols") or ())
        )
        return self.readiness_service.evaluate(
            ligand_scope=ligand_scope,
            receptor_scope=receptor_scope,
            protocols=protocols,
            program=inp["program"],
            skip_existing=bool(inp.get("skip_existing")),
        ).as_mapping()

    def _check_requirements(self) -> None:
        # Automatic + off-thread. Triggered on entering the step, whenever a run-scope combo
        # changes, and by the live poll. No busy overlay: the status line below says "Checking…",
        # and the counts on screen stay valid while the new ones are computed.
        if self.stepper.current_index != 3 or self._check_inflight:
            return  # single-flight: on a big set one count can outlast the poll interval
        self._check_inflight = True
        self.check_status_label.setText("Checking…")
        inputs = self._requirement_inputs()
        run_async(
            lambda: self._compute_requirement_counts(inputs),
            self._apply_check,
            on_error=lambda exc: self._apply_check({"error": str(exc)}),
        )

    def _apply_check(self, result: dict) -> None:
        self._check_inflight = False
        if "error" in result:  # only a genuine exception blanks everything
            for counter in (self.req_ligands_count, self.req_receptors_count):
                counter.setText("—")
            self.step_run.set_error(True)
            self.check_status_label.setText(result["error"])
            return
        lig, rec = result["ligands"], result["receptors"]
        self._set_sharded_run_mode(result.get("mode") == "sharded")
        if result.get("mode") == "sharded":
            self._set_offtarget_receptors([
                (int(receptor_id), str(name))
                for receptor_id, name in (result.get("ready_receptors") or ())
            ])
            records = int(result.get("records") or 0)
            self.req_ligands_count.setText(
                f"{lig['ready']} / {lig['total']} shard(s) prepared · {records} molecule(s)"
            )
            self.req_receptors_count.setText(f"{rec['ready']} / {rec['total']} prepared")
            receptors = max(0, int(rec.get("ready") or 0))
            reference_id = result.get("offtarget_reference_id")
            if reference_id is not None and receptors > 1:
                second_stage = min(int(self.hit_cap.value()), records) * (receptors - 1)
                self.req_pairs_count.setText(
                    f"{records} reference + up to {second_stage} off-target docking(s)"
                )
            else:
                chunks = max(0, int(lig.get("ready") or 0)) * receptors
                self.req_pairs_count.setText(
                    f"{records * receptors} docking(s) in {chunks} chunk(s)"
                )
            self.step_run.set_done(bool(result.get("ready")))
            self.check_status_label.setText(
                "Ready to run the campaign." if result.get("ready")
                else "Prepare the library and at least one receptor (with a grid) first."
            )
            return
        if result.get("mode") == "redocking":
            self.req_ligands_count.setText(f"{lig['ready']} / {lig['total']}")
            self.req_receptors_count.setText(f"{rec['ready']} / {rec['total']}")
            # Redocking runs the original pairs, so the pair count is the ready-complex count.
            self.req_pairs_count.setText(f"{int(rec.get('ready') or 0)} original pair(s)")
            self.step_run.set_done(result["ready"])
            notes = []
            if int(result.get("total_complexes") or 0) == 0:
                notes.append("no reference/redocking complexes are available")
            if int(lig.get("ready") or 0) < int(lig.get("total") or 0):
                notes.append("some reference ligands are not prepared")
            if int(rec.get("ready") or 0) < int(rec.get("total") or 0):
                notes.append("some original pairs are missing a prepared receptor or binding-site grid")
            failures = []
            if int(lig.get("failed") or 0):
                failures.append(f"{int(lig['failed'])} ligand preparation failed")
            if int(rec.get("failed") or 0):
                failures.append(f"{int(rec['failed'])} receptor preparation failed")
            if failures:
                notes.append("; ".join(failures))
            self.check_status_label.setText(
                f"Ready to redock {int(rec.get('ready') or 0)} original pair(s)."
                if result["ready"] and not notes
                else "; ".join(notes) + "."
            )
            return
        self.req_ligands_count.setText(f"{lig['ready']} / {lig['total']} prepared")
        self.req_receptors_count.setText(f"{rec['ready']} / {rec['total']} prepared")
        pairs = dict(result.get("pairs") or {})
        total_pairs = int(pairs.get("total") or 0)
        if not total_pairs:
            self.req_pairs_count.setText("—")
        else:
            done = int(pairs.get("already") or 0)
            text = f"{int(pairs.get('to_run') or 0)} to run of {total_pairs}"
            self.req_pairs_count.setText(f"{text} ({done} already docked)" if done else text)
        self.step_run.set_done(result["ready"])
        notes = []
        failures = []
        if int(lig.get("failed") or 0):
            failures.append(f"{int(lig['failed'])} ligand preparation failed")
        if int(rec.get("failed") or 0):
            failures.append(f"{int(rec['failed'])} receptor preparation failed")
        if failures:
            notes.append("; ".join(failures))
        if notes:
            self.check_status_label.setText("; ".join(notes) + ".")
        else:
            self.check_status_label.setText(
                "Ready to run." if result["ready"] else "Nothing prepared to dock yet on one side."
            )

    def _run_docking(self) -> None:
        # Off the GUI thread: the readiness counts (over the whole ligand set) and docking.run
        # (materializes pairs + writes complexes) are heavy and were freezing the UI. Capture all
        # widget state here, then check for a duplicate job, then submit — each step off-thread.
        inp, params = self._docking_params()
        if not params["protocols"]:
            self._warn("Run Docking", "Select at least one compatible docking software first.")
            return
        signature = self._docking_signature(inp, params)
        self.run_button.setEnabled(False)
        # No busy overlay here: the disabled Run button already says "working", and dimming the
        # scope counts implies THEY are being recomputed, which they aren't.
        run_async(
            lambda: self._check_docking_conflict(signature),
            lambda conflict: self._after_conflict_check(conflict, inp, params, signature),
            on_error=self._on_docking_error,
        )

    def _docking_params(self) -> tuple[dict, dict]:
        inp = self._requirement_inputs()
        protocols = self._selected_protocols()
        params = {
            "run_kind": inp["run_kind"],
            "sel_lig": inp["sel_lig"],
            "protocols": protocols,
            "batch_size": int(self.batch_size.value()),
            "hit_threshold": float(self.hit_threshold.value()),
            "hit_cap": (
                int(self.hit_cap.value())
                if str(self.hit_mode_combo.currentData() or "") == HIT_MODE_TOP_N
                else HIT_SAFETY_CAP
            ),
            "hit_mode": str(self.hit_mode_combo.currentData() or HIT_MODE_TOP_N),
            "offtarget_reference_id": (
                int(self.offtarget_reference_combo.currentData())
                if self.offtarget_reference_combo.currentData() is not None
                else None
            ),
            "executor_name": DEFAULT_LOCAL_CPU_EXECUTOR,
            "skip_existing": bool(self.skip_existing_check.isChecked()),
            "compute_interactions": bool(self.compute_interactions_check.isChecked()),
            "compute_diagram": bool(self.render_diagrams_check.isChecked()),
            "run_id": uuid4().hex,
        }
        return inp, params

    def workflow_step_payload(self) -> dict | None:
        """The pieces a workflow step needs from this panel's current settings (kind/name/category/
        submit), or None if the config is invalid (a warning is shown). Shared by the panel's own
        "Save to workflow" button and the workflow editor's config dialog (fresh panel instance)."""
        inp, params = self._docking_params()
        if not params["protocols"]:
            self._warn("Workflow", "Select at least one compatible docking software first.")
            return None
        workflow_kind = "redocking" if params.get("run_kind") == "redocking" else "docking"
        protocol_names = [str(protocol.get("label") or protocol.get("program") or "") for protocol in
                          params.get("protocols", [])]
        if params.get("run_kind") == "redocking":
            protocol_summary = protocol_names[0] if len(protocol_names) == 1 else f"{len(protocol_names)} protocols"
        else:
            protocol_summary = protocol_names[0] if len(protocol_names) == 1 else f"{len(protocol_names)} software jobs"
        return {
            "kind": workflow_kind,
            "category": "docking",
            "name": f"{'Redocking' if workflow_kind == 'redocking' else 'Docking'} ({protocol_summary})",
            "submit": lambda _rt, i=inp, p=params: self._workflow_submit_docking(i, p),
        }

    def _add_docking_to_workflow(self) -> None:
        from amdockvs.ui.tools.workflow_panel import save_to_workflow

        payload = self.workflow_step_payload()
        if payload is None:
            return
        # Upsert by kind: re-saving with new settings updates the pending docking step in place
        # instead of stacking duplicates (configure here, save to workflow).
        save_to_workflow(
            self.window(),
            kind=payload["kind"], name=payload["name"], category=payload["category"], submit=payload["submit"],
        )

    def _workflow_submit_docking(self, inp: dict, params: dict) -> list[str]:
        # Runs in the workflow's materialize worker (off-GUI). _submit_docking is DB/runtime-only.
        runtime_params = dict(params)
        runtime_params["run_id"] = uuid4().hex
        result = self._submit_docking(inp, runtime_params)
        if "warning" in result:
            raise RuntimeError(result["warning"])
        job_ids = list(result["job_ids"].values())
        if result.get("interaction_job_id"):
            job_ids.append(str(result["interaction_job_id"]))
        return job_ids

    @staticmethod
    def _docking_signature(inp: dict, params: dict) -> str:
        request = DockingRunRequest(
            run_kind=str(params.get("run_kind") or "docking"),
            ligand_scope=None,
            receptor_scope=None,
            protocols=tuple(
                DockingProtocol.from_mapping(value)
                for value in (params.get("protocols") or ())
            ),
            skip_existing=bool(params.get("skip_existing", True)),
        )
        identity = DockingRunIdentity(
            receptor_type=str(inp.get("receptor_type") or MoleculeType.PROTEIN),
            ligand_type=str(inp.get("ligand_type") or MoleculeType.SMALL_MOLECULE),
            # "selected" vs "all" is now just "is the table scoped?" — docking_signature only
            # keeps the ids in the selected case.
            ligand_mode="selected" if inp.get("sel_lig") else "all",
            ligand_ids=tuple(sorted(int(value) for value in (inp.get("sel_lig") or ()))),
            receptor_mode="selected" if inp.get("sel_rec") else "all",
            receptor_ids=tuple(sorted(int(value) for value in (inp.get("sel_rec") or ()))),
        )
        return (
            f"{docking_signature(request, identity)}:offtarget="
            f"{params.get('offtarget_reference_id') or 'none'}"
        )

    def _check_docking_conflict(self, signature: str) -> tuple[str, str]:
        return self.readiness_service.conflict(signature, self._docking_job_sigs)

    def _after_conflict_check(self, conflict: tuple[str, str], inp: dict, params: dict, signature: str) -> None:
        level, message = conflict
        if level == "error":
            self.run_button.setEnabled(True)
            self._warn("Run Docking", message)
            return
        if level == "warning":
            answer = QMessageBox.question(
                self, "Run Docking", f"{message}\n\nLaunch a new docking job anyway?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.run_button.setEnabled(True)
                return
        # Submit materializes every receptor×ligand pair before the job exists, so on a large set
        # this takes seconds. Feedback is the status line + the disabled Run button, not an
        # overlay over counts that stay valid the whole time.
        self.check_status_label.setText("Submitting docking job…")
        run_async(
            lambda: self._submit_docking(inp, params),
            lambda result: self._on_docking_submitted(result, signature),
            on_error=self._on_docking_error,
        )

    def _sync_hit_cap_label(self) -> None:
        """One criterion at a time: the other number is not a second filter, it is the safety net.

        Ranked keeps N and the threshold is only the coarse prefilter; by-threshold keeps
        everything under the cutoff, and capping *that* would silently drop hits it already
        found. So each mode enables its own number and leaves the other one out of the way.
        """
        ranked = str(self.hit_mode_combo.currentData() or "") == HIT_MODE_TOP_N
        if not ranked and self.offtarget_reference_combo.currentData() is not None:
            self.offtarget_reference_combo.blockSignals(True)
            self.offtarget_reference_combo.setCurrentIndex(0)
            self.offtarget_reference_combo.blockSignals(False)
        # The disabled field still shows what the run will use: ranked has no cutoff, so it
        # reads 0 (anything that bound at all), and the by-threshold value is kept for the way back.
        if ranked and self.hit_threshold.value() != RANKED_PREFILTER_SCORE:
            self._last_threshold = float(self.hit_threshold.value())
            self.hit_threshold.setValue(RANKED_PREFILTER_SCORE)
        elif not ranked and self.hit_threshold.value() == RANKED_PREFILTER_SCORE:
            self.hit_threshold.setValue(float(getattr(self, "_last_threshold", -8.0)))
        self.hit_cap.setEnabled(ranked)
        self.hit_cap_label.setEnabled(ranked)
        self.hit_threshold.setEnabled(not ranked)
        self.hit_threshold_label.setEnabled(not ranked)
        reference_id = self.offtarget_reference_combo.currentData()
        self.hit_cap_label.setText(
            "Reference Top-N" if ranked and reference_id is not None
            else "Keep best per receptor" if ranked
            else f"Ceiling ({HIT_SAFETY_CAP:,} hits)"
        )
        self.hit_cap.setToolTip(
            "How many hits the run keeps per receptor; with an off-target reference, this is "
            "the reference Top-N propagated to every secondary receptor."
            if ranked else
            f"Not a criterion here — by-threshold keeps every ligand under the cutoff. The run "
            f"still stops at {HIT_SAFETY_CAP:,} hits so a generous receptor cannot write the "
            f"whole library into the project unattended."
        )
        self.offtarget_reference_combo.setEnabled(ranked and len(self._ready_receptor_options) > 1)

    def _set_offtarget_receptors(self, receptors: list[tuple[int, str]]) -> None:
        """Refresh the small selector without losing the user's still-valid choice."""
        current = self.offtarget_reference_combo.currentData()
        self._ready_receptor_options = list(receptors)
        self.offtarget_reference_combo.blockSignals(True)
        self.offtarget_reference_combo.clear()
        self.offtarget_reference_combo.addItem(
            "None — dock the full library against every receptor", None
        )
        for receptor_id, name in receptors:
            self.offtarget_reference_combo.addItem(
                f"{name} (ID {receptor_id})" if name else f"Receptor {receptor_id}",
                receptor_id,
            )
        index = self.offtarget_reference_combo.findData(current)
        self.offtarget_reference_combo.setCurrentIndex(max(0, index))
        self.offtarget_reference_combo.blockSignals(False)
        self._sync_hit_cap_label()

    def _set_sharded_run_mode(self, sharded: bool) -> None:
        self.hits_box.setVisible(sharded)
        if not sharded and self.offtarget_reference_combo.currentIndex() != 0:
            self.offtarget_reference_combo.setCurrentIndex(0)
        self.batch_size_label.setVisible(not sharded)
        self.batch_size.setVisible(not sharded)

    def _submit_shard_docking(self, inp: dict, params: dict) -> dict:
        """The sharded twin of `_submit_docking`: no ligand scope, a hit gate instead.

        ponytail: it calls `runtime.docking.run_shards` directly rather than going through
        `DockingSubmissionService`, whose request object is built around ligand scopes and
        already-docked pairs. Move it there when a second caller (a workflow step) needs it.
        """
        counts = self._compute_requirement_counts(inp)
        ready_receptor_ids = tuple(int(v) for v in (counts.get("ready_receptor_ids") or ()) if int(v) > 0)
        if not int(counts["ligands"]["ready"]):
            return {"warning": "No prepared shards in the library — run ligand preparation first."}
        if not ready_receptor_ids:
            return {"warning": "No prepared receptors with a binding-site grid — nothing to dock."}
        protocols = tuple(DockingProtocol.from_mapping(value) for value in (params.get("protocols") or ()))
        if not protocols:
            return {"warning": "Select at least one compatible docking software first."}
        reference_id = params.get("offtarget_reference_id")
        if reference_id is not None and int(reference_id) not in ready_receptor_ids:
            return {"warning": "The selected off-target reference receptor is no longer ready."}
        if reference_id is not None and str(params.get("hit_mode")) != HIT_MODE_TOP_N:
            return {"warning": "Off-target reference docking requires ranked Top-N selection."}
        receptor_scope = self._resolve_receptor_scope(list(ready_receptor_ids))
        job_ids: dict[str, str] = {}
        for index, protocol in enumerate(protocols):
            config = dict(protocol.config)
            protocol_run_id = str(params["run_id"])
            if len(protocols) > 1:
                protocol_run_id = f"{protocol_run_id}-{protocol.hash[:12] or index + 1}"
            common = {
                "program": protocol.program,
                "hit_threshold": float(params["hit_threshold"]),
                "hit_cap": int(params["hit_cap"]),
                "hit_mode": str(params.get("hit_mode") or HIT_MODE_THRESHOLD),
                "exhaustiveness": int(config.get("exhaustiveness") or 8),
                "num_modes": int(config.get("num_modes") or 9),
                "scoring_function": str(config.get("scoring_function") or "vina"),
                "vina_backend": str(config.get("vina_backend") or "binary"),
                "vina_cpu": int(config.get("vina_cpu") or 1),
                "executor_name": str(params["executor_name"]),
                "skip_existing": bool(params.get("skip_existing", True)),
                "run_id": protocol_run_id,
                "protocol_metadata": protocol.to_mapping(),
                "engine_config": config,
            }
            key = protocol_job_key(protocol, index)
            if reference_id is None or len(ready_receptor_ids) < 2:
                job_ids[key] = self.runtime.docking.run_shards(
                    receptor_set=receptor_scope,
                    **common,
                )
                continue
            reference_id = int(reference_id)
            reference_job = self.runtime.docking.run_shards(
                receptor_set=self._resolve_receptor_scope([reference_id]),
                **common,
            )
            job_ids[f"{key}:reference"] = reference_job
            off_target_ids = [value for value in ready_receptor_ids if value != reference_id]
            job_ids[f"{key}:off-target"] = self.runtime.docking.run_shards(
                receptor_set=self._resolve_receptor_scope(off_target_ids),
                depends_on=[reference_job],
                selected_from_receptor_id=reference_id,
                selected_top_n=int(params["hit_cap"]),
                **common,
            )
        return {
            "job_ids": job_ids,
            "interaction_job_id": "",
            "receptor_ids": list(ready_receptor_ids),
            "complex_ids": [],
            "params": params,
        }

    def _submit_docking(self, inp: dict, params: dict) -> dict:
        # Worker thread: services perform DB/runtime work; no widget access occurs here.
        if inp.get("sharded"):
            return self._submit_shard_docking(inp, params)
        counts = self._compute_requirement_counts(inp)
        protocols = tuple(
            DockingProtocol.from_mapping(value)
            for value in (params.get("protocols") or ())
        )
        if not protocols:
            return {"warning": "Select at least one compatible docking software first."}
        ready_receptor_ids = tuple(
            int(value) for value in (counts.get("ready_receptor_ids") or ()) if int(value) > 0
        )
        complex_ids = tuple(
            int(value) for value in (counts.get("ready_complex_ids") or ()) if int(value) > 0
        )
        if params.get("run_kind") == "redocking":
            ligand_scope = receptor_scope = None
            if not complex_ids:
                return {"warning": "No prepared original receptor-ligand pairs are ready for redocking."}
        else:
            ligands_ready = int(counts["ligands"]["ready"])
            if ligands_ready == 0 or not ready_receptor_ids:
                missing = []
                if ligands_ready == 0:
                    missing.append("prepared ligands")
                if not ready_receptor_ids:
                    missing.append("prepared receptors (with a grid)")
                return {"warning": f"No {' or '.join(missing)} in scope — nothing to dock."}
            ligand_scope = self._resolve_ligand_scope(params["sel_lig"], run_kind=params.get("run_kind", "docking"))
            receptor_scope = self._resolve_receptor_scope(list(ready_receptor_ids))
        request = DockingRunRequest(
            run_kind=str(params.get("run_kind") or "docking"),
            ligand_scope=ligand_scope,
            receptor_scope=receptor_scope,
            protocols=protocols,
            skip_existing=bool(params.get("skip_existing", True)),
            batch_size=int(params["batch_size"]),
            executor_name=str(params["executor_name"]),
            run_id=str(params["run_id"]),
            compute_interactions=bool(params.get("compute_interactions")),
            compute_diagram=bool(params.get("compute_diagram")),
            complex_ids=complex_ids,
        )
        try:
            submission = self.submission_service.submit(
                request,
                ready_receptor_ids=ready_receptor_ids,
            )
        except ValueError as exc:
            return {"warning": str(exc)}
        return {
            "job_ids": submission.job_ids,
            "interaction_job_id": submission.interaction_job_id,
            "receptor_ids": list(submission.receptor_ids),
            "complex_ids": list(submission.complex_ids),
            "params": params,
        }

    def _on_docking_submitted(self, result: dict, signature: str) -> None:
        self.run_button.setEnabled(True)
        if "warning" in result:
            self._warn("Run Docking", result["warning"])
            return
        # Remember each launched job's signature so a later launch can detect a duplicate.
        for job_id in result["job_ids"].values():
            self._docking_job_sigs[str(job_id)] = signature
        params = result["params"]
        self._append_status(
            "Docking Submitted",
            {
                "job_ids": result["job_ids"],
                "interaction_job_id": result.get("interaction_job_id") or "",
                "receptor_ids": result["receptor_ids"],
                "complex_ids": result.get("complex_ids") or [],
                "run_kind": params.get("run_kind") or "docking",
                "executor": params["executor_name"],
                "batch_size": params["batch_size"],
                "protocols": len(params.get("protocols") or []),
            },
        )
        # The run is launched: the studio has nothing left to configure, so it gets out of the
        # way and leaves the screen to the results it is feeding. Closing the tools dock is what
        # retires the tool (see _on_tools_dock_visibility); it deletes THIS widget, so it goes
        # last — deleteLater is deferred, but nothing may run after it here.
        from amdockvs.ui.catalog.domain_views import COMPLEXES_VIEW_ID  # circular at import time

        window = self.window()
        opener = getattr(window, "open_or_focus_view", None)
        if callable(opener):
            opener(COMPLEXES_VIEW_ID)
        manager = getattr(window, "dock_manager", None)
        if manager is not None:
            manager.toggle("tools", False)

    def _on_docking_error(self, exc: Exception) -> None:
        self.run_button.setEnabled(True)
        if isinstance(exc, ValueError):
            self._warn("Run Docking", str(exc))
        else:
            self._error("Run Docking", exc)
