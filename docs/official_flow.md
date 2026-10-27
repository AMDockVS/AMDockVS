# Official Flow

Flujo canónico actual para virtual screening con Vina:

1. `runtime.loader.load_ligands(...)`
2. `runtime.loader.load_receptors(...)`
3. pasos opcionales de ligando: `runtime.chemistry.standardize_ligands(...)`, `protonate_ligands(...)`, `generate_3d_ligands(...)`, `minimize_ligands(...)`
4. pasos opcionales de receptor: `runtime.chemistry.protonate_receptors(...)`, `minimize_receptors(...)`
5. `runtime.qsar.compute_descriptors(...)`
6. `runtime.docking.prepare_ligands(...)`
7. `runtime.docking.prepare_receptors(...)`
8. `runtime.docking.set_grid(...)` o `runtime.docking.run(box_center=..., box_size=...)`
9. `runtime.docking.run(...)`

Los pasos de `runtime.chemistry` son herramientas independientes. No forman un workflow automático; cada notebook decide cuáles aplicar y en qué orden.

Helper compuesto:

```python
summary = runtime.docking.screen(
    ligand_files=[...],
    receptor_files=[...],
    box_center=(12.0, 13.0, 10.0),
    box_size=(20.0, 20.0, 20.0),
    wait=True,
)
```

`screen(...)` es async por defecto y retorna los `job_id` de import, descriptores, preparación y docking dentro de `ScreeningSummary`.
