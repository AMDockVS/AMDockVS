from types import SimpleNamespace

from amdockvs.io import downloads
from amdockvs.io.downloads import SOURCES, discard_temporary, download, parse_ids, sources_for, stored_files
from amdockvs.io.jobs import drop_temporary_files


def test_ids_split_per_source_and_urls_come_out_of_a_pasted_script():
    assert parse_ids(SOURCES["pdb"], "1IEP, 4hjo\n1IEP;2SRC") == ["1IEP", "4hjo", "2SRC"]
    # PubChem names carry spaces: one per line.
    assert parse_ids(SOURCES["pubchem"], "2244\nacetylsalicylic acid\n") == ["2244", "acetylsalicylic acid"]
    script = "wget https://a.org/x/H17P050.smi.gz -O a\ncurl 'https://b.org/lib.sdf' > b"
    assert parse_ids(SOURCES["url"], script) == ["https://a.org/x/H17P050.smi.gz", "https://b.org/lib.sdf"]
    assert {s.key for s in sources_for("receptor")} == {"pdb", "alphafold", "esm_atlas", "uniprot", "url"}
    assert "pdb" not in {s.key for s in sources_for("ligand")}


def test_pubchem_records_take_the_typed_name():
    sdf = "2244\n  PubChem\n\nM  END\n$$$$\n5291\n  PubChem\n\nM  END\n$$$$\n"
    renamed = downloads._sdf_titled(sdf, "aspirin")
    assert renamed.count("$$$$") == 2
    assert [block.lstrip("\n").split("\n", 1)[0] for block in renamed.split("$$$$")[:2]] == ["aspirin", "aspirin"]


def test_a_downloaded_file_is_reused_and_an_id_that_fails_does_not_sink_the_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path))
    cached = tmp_path / "downloads" / "pdb" / "1IEP.cif"
    cached.parent.mkdir(parents=True)
    cached.write_text("data_1IEP\n")

    def offline(url):
        raise OSError(f"offline: {url}")

    monkeypatch.setattr(downloads, "_open", offline)
    outcome = download("pdb", ["1iep", "4hjo"])  # no progress callback: the tqdm bar
    assert outcome.files == [(str(cached), "RCSB PDB:1IEP")]
    assert len(outcome.failures) == 1 and outcome.failures[0].startswith("4HJO: offline")
    assert not list(cached.parent.glob("*.part"))


def test_temporary_downloads_go_once_their_import_completed_and_nothing_else_does(tmp_path):
    project = tmp_path / "project"
    folder = downloads.temporary_folder(project, SOURCES["pdb"])
    temp = downloads._write(folder / "1IEP.cif", "data_1IEP\n")
    picked = downloads._write(project / "mine.cif", "data_X\n")
    assert downloads.temporary_in([temp, picked], project) == [str(temp)]
    assert [(key, size) for _path, key, size in stored_files(downloads.temporary_root(project))] == [("pdb", 10)]

    context = SimpleNamespace(path=str(project))
    params = {"temporary_files": [str(temp), str(picked)]}
    for status in ("failed", "canceled"):  # kept: a resume reads them again
        drop_temporary_files(params, {"project_context": context, "terminal_status": status})
        assert temp.exists()
    drop_temporary_files(params, {"project_context": context, "terminal_status": "completed"})
    assert not temp.exists() and picked.exists()
    assert list(downloads.temporary_root(project).iterdir()) == []  # its batch folder went too

    again = downloads._write(downloads.temporary_folder(project, SOURCES["pdb"]) / "2SRC.cif", "x")
    discard_temporary([again, picked])
    assert not again.exists() and picked.exists()


def test_a_labelled_temporary_receptor_with_a_ligand_imports_and_its_file_goes(tmp_path, monkeypatch):
    """The label names the molecules; the complex still finds its receptor by the file."""
    from pathlib import Path

    from amdockvs.runtime import AMDockVSRuntime

    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="dl", folder=tmp_path / "dl", description="")
        root = runtime.get_project_paths()["project_root"]
        protein = "".join(
            f"ATOM  {i + 1:5d}  CA  ALA A{i + 1:4d}    {10.0 + i * 3.8:8.3f}{10.0:8.3f}{10.0:8.3f}  1.00 20.00           C\n"
            for i in range(30)
        )
        ligand = [line for line in (Path(__file__).parent / "data" / "ccd_ligand.pdb").read_text().splitlines()
                  if line.startswith("HETATM")]
        receptor = downloads._write(downloads.temporary_folder(root, SOURCES["pdb"]) / "1ABC.pdb",
                                    protein + "\n".join(ligand) + "\nEND\n")
        jobs = runtime.loader.load_receptors([receptor], source_labels={str(receptor): "RCSB PDB:1ABC"})
        status = runtime.wait_for_jobs(jobs, timeout_s=120, poll_s=0.05)[jobs[0]]
        assert status.status == "completed", status.error
        with runtime.workspace.project_db.get_session() as session:
            sql = session.connection().exec_driver_sql
            assert {row[0] for row in sql("SELECT b.source FROM molecules m JOIN import_batches b ON b.id = m.import_batch_id").fetchall()} == {"RCSB PDB:1ABC"}
            assert sql("SELECT COUNT(*) FROM complexes").scalar() >= 1
        assert not receptor.exists()
    finally:
        runtime.shutdown()
