from __future__ import annotations

import locale

import pydantic.fields  # noqa: F401 - load before PySide6's feature importer
from PySide6.QtCore import QLocale
from PySide6.QtWidgets import QApplication, QDoubleSpinBox

from amdockvs import app


def test_numbers_use_a_dot_whatever_the_os_locale():
    QApplication.instance() or QApplication([])
    QLocale.setDefault(QLocale(QLocale.Language.Spanish, QLocale.Country.Colombia))
    saved = locale.setlocale(locale.LC_NUMERIC)
    try:
        app.use_en_us_numbers()
        spin = QDoubleSpinBox()
        spin.setValue(18.93)
        assert spin.text() == "18.93"
        assert locale.localeconv()["decimal_point"] == "."
    finally:
        QLocale.setDefault(QLocale.system())
        locale.setlocale(locale.LC_NUMERIC, saved)
