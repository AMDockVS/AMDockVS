import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.docking.engines.gnina import GNINA, chunk_gpu_tokens, normalize_cnn_mode, run_gnina_docking_rows

DATA = Path(__file__).resolve().parents[1] / "data" / "docking"


def test_normalize_and_gpu_tokens():
    # scoring_function default "vina" is meaningless to gnina -> rescore; gpu tokens only for GPU-bound modes.
    assert normalize_cnn_mode("vina") == "rescore"
    assert normalize_cnn_mode("ALL") == "all"
    assert chunk_gpu_tokens("gnina", "rescore") == {}
    assert chunk_gpu_tokens("gnina", "all") == {"_gpu_required": 1}
    assert chunk_gpu_tokens("gnina", "refinement") == {"_gpu_required": 1}
    assert chunk_gpu_tokens("vina", "vina") == {}


def test_two_protocols_of_a_pair_do_not_share_their_pose_file():
    from amdockvs.docking.results.metrics import pose_file_stem

    pair = {"ligand_id": 1, "receptor_id": 2}
    assert pose_file_stem(pair, None) == "1__2"
    assert pose_file_stem({**pair, "complex_id": 7, "run_kind": "redocking"}, {"hash": ""}) == "redocking_7"
    assert pose_file_stem(pair, {"hash": "a" * 40}) != pose_file_stem(pair, {"hash": "b" * 40})


@pytest.mark.skipif(not shutil.which(GNINA) and not Path(GNINA).exists(), reason="gnina binary not installed")
def test_gnina_docks_1iep(tmp_path):
    rows = run_gnina_docking_rows(
        {
            "pairs": [
                {
                    "ligand_id": 1,
                    "receptor_id": 2,
                    "ligand_path": str(DATA / "1iep_ligand.sdf"),
                    "receptor_path": str(DATA / "1iep_receptor.pdbqt"),
                    "num_modes": 5,
                    "box_center": [15.190, 53.903, 16.917],
                    "box_size": [20.0, 20.0, 20.0],
                }
            ],
            "output_dir": str(tmp_path),
            "scoring_function": "rescore",
            "vina_cpu": 2,
            "seed": 42,
        }
    )
    assert rows, "gnina produced no rows"
    best = rows[0]
    assert best["engine"] == "gnina"
    assert best["score_type"] == "gnina_score"
    assert isinstance(best["score"], float)  # minimizedAffinity, kcal/mol
    assert isinstance(best["metrics"]["CNNaffinity"], float)  # CNN ran on GPU
