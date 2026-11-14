"""A FASTA imported as Nucleotide takes nucleic-acid letters only.

Worth a test because nothing else stops it: the parser keeps every letter, so ACGTEFILPZ was
stored as a DNA sequence that no tool downstream can use.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.io.import_stats import INVALID_SEQUENCE, summarize
from amdockvs.io.parsers.readers import _iter_fasta_records
from amdockvs.io.transformers.materializers import materialize_import_batch


def _import(tmp_path, text, molecule_kind):
    fasta = tmp_path / "seqs.fasta"
    fasta.write_text(text, encoding="utf-8")
    payload = {
        "kind": "ligand",
        "file_path": str(fasta),
        "storage_dir": str(tmp_path / "data"),
        "input_format": "fasta",
        "primary_role": "ligand",
        "molecule_kind": molecule_kind,
        "entries": list(_iter_fasta_records(fasta)),  # what the import feed hands over
    }
    return {row["name"]: row["sequence_aa"] for row in materialize_import_batch(payload)}


def test_a_nucleotide_import_skips_a_record_with_other_letters(tmp_path):
    text = ">dna\nACGTACGT\n>rna\nacgu\n>ambiguous\nACGTNRYSWKMBDHV\n>complex\nACGT:TGCA\n>bad\nACGTEFILPZ\n"
    assert _import(tmp_path, text, "nucleotide") == {
        "dna": "ACGTACGT",
        "rna": "ACGU",
        "ambiguous": "ACGTNRYSWKMBDHV",
        "complex": "ACGT:TGCA",
    }


def test_a_protein_import_takes_every_letter(tmp_path):
    # B, J, O, U, X, Z are all amino-acid codes, so nothing here is out of the alphabet.
    assert _import(tmp_path, ">p\nACGTEFILPZBJOUX\n", "protein") == {"p": "ACGTEFILPZBJOUX"}


def test_the_summary_says_why_it_was_skipped():
    assert summarize({"imported": 1, INVALID_SEQUENCE: 1}) == "Imported 1 of 2 - 1 skipped (1 invalid sequence)."
