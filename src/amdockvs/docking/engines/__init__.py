"""Docking programs and the runners that execute them.

One engine = one program spec (metadata, defaults, resources) plus one chunk runner
`(payload) -> list[row]`. `registry` is the only door: adding an engine means calling
`register_docking_engine`, never editing a match statement.

Paths an engine writes into a file
----------------------------------
Never write an absolute path into a file a program parses (its ligand index, file list or
config). Write bare file names -- or paths relative to the folder the program runs in -- and
give `run_program`/`_run_vina` that folder as `cwd`.

A path handed over as an argument is safe: we run programs without a shell, so each argument
arrives whole however it is spelled. Inside a file that guarantee is gone, because the program
parses it with its own code. Uni-Dock splits each line of `--ligand_index` on whitespace, so a
project named "AmpC DUDE-Z virtual screening (Unidock)" -- the folder is the project's name --
made it abort on the first ligand and every pair of the task failed. AutoDock-GPU reads whole
lines today, which is its choice to revisit, not a promise to us. Project names are free text
and Windows home directories hold spaces, so the path is never ours to control.

`unidock` (index), `adgpu` (file list) and `autogrid` (GPF) all follow this; each has a test
that runs it from a folder whose name holds spaces and parentheses.
"""
