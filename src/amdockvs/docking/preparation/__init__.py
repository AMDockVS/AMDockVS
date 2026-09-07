"""Turning stored molecules into what an engine can read.

Profiles select the transformation (`ad4` today), `jobs` runs it over database rows and
`shards` over a screening container. `meeko` is the actual PDBQT writer.
"""
