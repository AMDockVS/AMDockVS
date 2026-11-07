# Official workflow

Current canonical workflow for virtual screening with Vina:

1. `runtime.loader.load_ligands(...)`
2. `runtime.loader.load_receptors(...)`
3. Optional ligand steps: `runtime.chemistry.standardize_ligands(...)`, `protonate_ligands(...)`, `generate_3d_ligands(...)`, `minimize_ligands(...)`
4. Optional receptor steps: `runtime.chemistry.protonate_receptors(...)`, `minimize_receptors(...)`
5. `runtime.qsar.compute_descriptors(...)`
6. `runtime.docking.prepare_ligands(...)`
7. `runtime.docking.prepare_receptors(...)`
8. `runtime.docking.set_grid(...)` o `runtime.docking.run(box_center=..., box_size=...)`
9. `runtime.docking.run(...)`

The `runtime.chemistry` steps are independent tools. They do not form an automatic workflow; each notebook decides which ones to apply and in what order.

Combined helper:

```python
summary = runtime.docking.screen(
    ligand_files=[...],
    receptor_files=[...],
    box_center=(12.0, 13.0, 10.0),
    box_size=(20.0, 20.0, 20.0),
    wait=True,
)
```

`screen(...)` is asynchronous by default and returns the `job_id` values for import, descriptors, preparation, and docking in `ScreeningSummary`.
