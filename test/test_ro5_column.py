"""`molecules.ro5_violations` is a VIRTUAL generated column: SQLite's count must be the
Python rule's (`_ro5_violations`), NULL descriptors included."""
import itertools

from ms_flow.core.database import ProjectStore
from sqlmodel import select

from amdockvs.chemistry.filtering import _ro5_violations
from amdockvs.models.molecules import MoleculeRecord


def test_sql_count_matches_the_python_rule(tmp_path):
    store = ProjectStore.open_at(tmp_path / "project.db")
    cases = list(itertools.product((None, 300.0, 501.0), (None, 2.0, 5.5), (None, 5, 6), (None, 10, 11)))
    try:
        with store.get_session() as session:
            session.add_all(MoleculeRecord(mw=mw, logp=logp, hbd=hbd, hba=hba) for mw, logp, hbd, hba in cases)
            session.commit()
            got = session.exec(select(MoleculeRecord.ro5_violations).order_by(MoleculeRecord.id)).all()
    finally:
        ProjectStore.clear_cached_stores()
    want = [len(_ro5_violations({"mw": mw, "logp": logp, "hbd": hbd, "hba": hba})) for mw, logp, hbd, hba in cases]
    assert got == want
