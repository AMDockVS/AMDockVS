from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from amdockvs.models import BindingSite, ComplexRecord, MoleculeRecord
from amdockvs.molecule_paths import get_default_project_root
class CatalogDetailsView(QWidget):
    """Molecule/complex inspector. Lives as a central tab that follows the catalog
    selection while open (was a bottom dock)."""

    show_molecule_requested = Signal(object, str)
    show_binding_site_requested = Signal(object, object)
    show_complex_requested = Signal(object)
    show_file_requested = Signal(str, str)

    def __init__(self, *, runtime, parent: QWidget | None = None):
        super().__init__(parent)
        self.runtime = runtime
        self._current_kind: str = ""
        self._current_molecule: MoleculeRecord | None = None
        self._current_complex: ComplexRecord | None = None
        self._current_binding_sites: list[BindingSite] = []
        root = self  # ponytail: build straight onto the widget, no wrapper child
        layout = QVBoxLayout(root)

        actions = QHBoxLayout()
        self.show_primary_button = QPushButton("Show", root)
        self.show_primary_button.clicked.connect(self._emit_primary_action)
        self.show_secondary_button = QPushButton("Show", root)
        self.show_secondary_button.clicked.connect(self._emit_secondary_action)
        self.show_binding_site_button = QPushButton("Show Binding Site", root)
        self.show_binding_site_button.clicked.connect(self._emit_binding_site_action)
        actions.addWidget(self.show_primary_button)
        actions.addWidget(self.show_secondary_button)
        actions.addWidget(self.show_binding_site_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        splitter = QSplitter(Qt.Vertical, root)
        self.summary_tree = QTreeWidget(splitter)
        self.summary_tree.setHeaderLabels(["Field", "Value"])
        self.summary_tree.setRootIsDecorated(True)
        self.summary_tree.header().setStretchLastSection(True)
        self.summary_tree.itemSelectionChanged.connect(self._sync_actions)

        self.json_text = QTextEdit(splitter)
        self.json_text.setReadOnly(True)
        self.json_text.setPlaceholderText("No selection")

        splitter.addWidget(self.summary_tree)
        splitter.addWidget(self.json_text)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)
        self._sync_actions()

    def show_molecule(self, molecule: MoleculeRecord) -> None:
        details = self.runtime.molecules.details(int(molecule.id or 0))
        if details is None:
            self.clear_details()
            return
        molecule = details.molecule
        binding_sites = details.binding_sites
        receptor_complexes = details.receptor_complexes
        ligand_complexes = details.ligand_complexes
        activities = details.activities
        self._current_kind = "molecule"
        self._current_molecule = molecule
        self._current_complex = None
        self._current_binding_sites = binding_sites
        self.summary_tree.clear()
        self._add_section(
            "Summary",
            {
                "ID": molecule.id,
                "Name": molecule.name,
                "Type": molecule.molecule_type,
                "Usage": getattr(molecule, "usage_class", ""),
                "Ligand": bool(molecule.is_ligand),
                "Receptor": bool(molecule.is_receptor),
                "Excluded": bool(molecule.excluded),
                "Has 3D": bool(molecule.has_3d),
                "Has Activity": bool(molecule.has_activity),
                "Active BS": getattr(molecule, "active_binding_site_id", None),
            },
        )
        self._add_section(
            "Paths",
            {
                "Stored": molecule.stored_path,
                "Current": molecule.current_path,
                "Source": molecule.source,
            },
        )
        if binding_sites:
            section = QTreeWidgetItem(["Binding Sites", str(len(binding_sites))])
            self.summary_tree.addTopLevelItem(section)
            for site in binding_sites:
                section.addChild(self._binding_site_item(site))
        if receptor_complexes or ligand_complexes:
            self._add_section(
                "Complexes",
                {
                    "As Receptor": len(receptor_complexes),
                    "As Ligand": len(ligand_complexes),
                },
            )
        if activities:
            latest = activities[0]
            self._add_section(
                "Activity",
                {
                    "Latest Value": latest.value,
                    "Type": latest.activity_type,
                    "Unit": latest.unit,
                },
            )
        self.json_text.setPlainText(json.dumps(dict(molecule.extra_data or {}), indent=2, ensure_ascii=False, default=str))
        self._sync_actions()

    def show_complex(self, pair: ComplexRecord) -> None:
        details = self.runtime.complexes.details(int(pair.id or 0))
        if details is None:
            self.clear_details()
            return
        pair = details.complex
        receptor = details.receptor
        ligand = details.ligand
        activity = details.activity
        reference_path = self._resolve_reference_path(str(getattr(pair, "reference_receptor_path", "") or ""))
        self._current_kind = "complex"
        self._current_molecule = None
        self._current_complex = pair
        self._current_binding_sites = []
        self.summary_tree.clear()
        self._add_section(
            "Complex",
            {
                "ID": pair.id,
                "Name": pair.name,
                "Purpose": pair.purpose,
                "Binding Site": pair.binding_site_id,
                "Receptor ID": pair.receptor_molecule_id,
                "Reference Receptor Path": getattr(pair, "reference_receptor_path", ""),
                "Ligand ID": pair.ligand_molecule_id,
                "Activity ID": pair.activity_id,
            },
        )
        if receptor is not None:
            self._add_section("Processed Receptor", {"Name": receptor.name, "Current": receptor.current_path})
        if reference_path is not None:
            self._add_section("Reference Receptor", {"Path": str(reference_path)})
        if ligand is not None:
            self._add_section("Reference Ligand", {"Name": ligand.name, "Stored": ligand.stored_path})
        if activity is not None:
            self._add_section("Activity", {"Value": activity.value, "Type": activity.activity_type, "Unit": activity.unit})
        metadata_text = str(pair.metadata_json or "").strip() or "{}"
        try:
            metadata = json.loads(metadata_text)
        except json.JSONDecodeError:
            metadata = {"raw": metadata_text}
        self.json_text.setPlainText(json.dumps(metadata, indent=2, ensure_ascii=False, default=str))
        self._sync_actions()

    def clear_details(self) -> None:
        self._current_kind = ""
        self._current_molecule = None
        self._current_complex = None
        self._current_binding_sites = []
        self.summary_tree.clear()
        self.json_text.clear()
        self._sync_actions()

    def _add_section(self, title: str, values: dict[str, object]) -> None:
        section = QTreeWidgetItem([str(title), ""])
        self.summary_tree.addTopLevelItem(section)
        for key, value in values.items():
            section.addChild(QTreeWidgetItem([str(key), "" if value is None else str(value)]))
        section.setExpanded(True)

    def _binding_site_item(self, site: BindingSite) -> QTreeWidgetItem:
        item = QTreeWidgetItem(
            [
                str(site.name or site.source or "site"),
                f"{site.source} | {site.source_ref}",
            ]
        )
        item.setData(0, Qt.UserRole, ("binding_site", int(site.id or 0)))
        return item

    def _selected_binding_site(self) -> BindingSite | None:
        selected = self.summary_tree.selectedItems()
        if selected:
            payload = selected[0].data(0, Qt.UserRole)
            if isinstance(payload, tuple) and len(payload) == 2 and payload[0] == "binding_site":
                site_id = int(payload[1] or 0)
                for site in self._current_binding_sites:
                    if int(site.id or 0) == site_id:
                        return site
        if self._current_molecule is not None:
            active_id = int(getattr(self._current_molecule, "active_binding_site_id", 0) or 0)
            if active_id > 0:
                for site in self._current_binding_sites:
                    if int(site.id or 0) == active_id:
                        return site
        return self._current_binding_sites[0] if self._current_binding_sites else None

    def _sync_actions(self) -> None:
        primary_text = "Show"
        secondary_text = "Show"
        primary_enabled = False
        secondary_enabled = False
        binding_enabled = False

        if self._current_kind == "molecule" and self._current_molecule is not None:
            if bool(getattr(self._current_molecule, "is_receptor", False)):
                primary_text = "Show Current"
                secondary_text = "Show Stored"
            else:
                primary_text = "Show Current"
                secondary_text = "Show Stored"
            primary_enabled = True
            secondary_enabled = True
            binding_enabled = self._selected_binding_site() is not None
        elif self._current_kind == "complex" and self._current_complex is not None:
            primary_text = "Show Pair"
            secondary_text = "Show Reference"
            primary_enabled = True
            secondary_enabled = bool(str(getattr(self._current_complex, "reference_receptor_path", "") or "").strip())

        self.show_primary_button.setText(primary_text)
        self.show_secondary_button.setText(secondary_text)
        self.show_primary_button.setEnabled(primary_enabled)
        self.show_secondary_button.setEnabled(secondary_enabled)
        self.show_binding_site_button.setEnabled(binding_enabled)

    def _emit_primary_action(self) -> None:
        if self._current_kind == "molecule" and self._current_molecule is not None:
            self.show_molecule_requested.emit(self._current_molecule, "current")
            return
        if self._current_kind == "complex" and self._current_complex is not None:
            self.show_complex_requested.emit(self._current_complex)

    def _emit_secondary_action(self) -> None:
        if self._current_kind == "molecule" and self._current_molecule is not None:
            self.show_molecule_requested.emit(self._current_molecule, "stored")
            return
        if self._current_kind == "complex" and self._current_complex is not None:
            reference_path = self._resolve_reference_path(str(getattr(self._current_complex, "reference_receptor_path", "") or ""))
            if reference_path is not None:
                self.show_file_requested.emit(str(reference_path), f"complex_reference_{int(self._current_complex.id or 0)}")

    def _emit_binding_site_action(self) -> None:
        if self._current_kind != "molecule" or self._current_molecule is None:
            return
        site = self._selected_binding_site()
        if site is None:
            return
        self.show_binding_site_requested.emit(self._current_molecule, site)

    @staticmethod
    def _resolve_reference_path(raw_path: str) -> Path | None:
        text = str(raw_path or "").strip()
        if not text:
            return None
        path = Path(text).expanduser()
        if path.is_absolute():
            return path
        project_root = get_default_project_root()
        return (project_root / path).resolve() if project_root is not None else path.resolve()


__all__ = ["CatalogDetailsView"]
