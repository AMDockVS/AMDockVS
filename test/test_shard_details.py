"""A clicked catalog file shows its own row plus the file header — metadata only, no records."""
from pathlib import Path
from types import SimpleNamespace
import sys

import pydantic.fields  # noqa: F401 - before PySide6: shiboken breaks pydantic's lazy imports
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ms_flow.core.data.shard import Serializer, ShardWriter
from amdockvs.core.constants import ShardKind

from ms_flow.core.database.project_models import CatalogFile
from amdockvs.ui.catalog.details import CatalogDetailsView, _shard_header


def _written_shard(tmp_path: Path) -> Path:
    path = tmp_path / "shard_0.arrows"
    with ShardWriter(
        path,
        dataset_id="00000000-0000-0000-0000-0000000000ff",
        shard_id=0,
        base_id=0,
        kind=ShardKind.SMILES,
        serializer=Serializer.UTF8,
        slot_count=3,
        metadata={"source_indices": [7, 8, 9]},
    ) as writer:
        for logical_id, smiles in enumerate(["CCO", "CCC", "c1ccccc1"]):
            writer.add(logical_id, smiles.encode())
    return path


def _tree_text(view) -> str:
    tree = view.summary_tree
    rows = []
    for i in range(tree.topLevelItemCount()):
        section = tree.topLevelItem(i)
        rows += [f"{section.child(j).text(0)}={section.child(j).text(1)}" for j in range(section.childCount())]
    return "\n".join(rows)


def test_header_reports_what_the_file_holds(tmp_path):
    header = _shard_header(_written_shard(tmp_path))
    assert header["record_count"] == 3
    assert header["metadata"]["source_indices"] == [7, 8, 9]


def test_a_missing_file_explains_itself_instead_of_raising(tmp_path):
    assert "not found" in _shard_header(tmp_path / "gone.arrows")["error"]


def test_details_show_the_row_and_the_header(tmp_path):
    QApplication.instance() or QApplication(["amdockvs-shard-details-test"])
    path = _written_shard(tmp_path)
    molecules = SimpleNamespace(file_preps=lambda file_id: {"ad4": (2, 1)} if file_id == 4 else {},
                                file_prep_failures=lambda file_id: {"no 3D": 1} if file_id == 4 else {})
    view = CatalogDetailsView(runtime=SimpleNamespace(molecules=molecules))
    view.show_shard(CatalogFile(id=4, generation_id=1, path=str(path), status="ready",
                                n_shards=1, n_records=3, n_active=2, scopes="batch-a"))
    text = _tree_text(view)
    assert "Total=3" in text and "Active=2" in text and "Shards=1" in text and "bytes" in text
    assert "ad4=2 record(s) · 1 failed" in text  # prepared or not, like Ligands/Receptors
    assert "no 3D=1" in text  # why, from the prepared file's dropped sidecar
    assert view._current_kind == "shard"
    assert '"record_count": 3' in view.json_text.toPlainText()
    assert not view.show_primary_button.isEnabled()  # nothing to draw for a file
