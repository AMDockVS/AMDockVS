"""Docking programs and the runners that execute them.

One engine = one program spec (metadata, defaults, resources) plus one chunk runner
`(payload) -> list[row]`. `registry` is the only door: adding an engine means calling
`register_docking_engine`, never editing a match statement.
"""
