"""Adapters for external programs and environments that no single feature owns.

A feature that wraps exactly one tool keeps its own adapter next to it
(binding_sites/p2rank.py, docking/preparation/meeko.py). This package holds the
shared contracts, the managed-tool registry and the environment bootstrap.
"""
