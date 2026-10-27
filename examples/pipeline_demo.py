from pathlib import Path

from amdockvs import AMDockVSRuntime


def main():
    box_center = (12.0, 13.0, 10.0)
    box_size = (20.0, 20.0, 20.0)
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="demo", folder=Path("./demo_project"))

        load_jobs = [
            *runtime.loader.load_ligands(["./inputs/ligands.sdf"], executor_name="thread"),
            *runtime.loader.load_receptors(["./inputs/receptor.pdb"], executor_name="thread"),
        ]
        runtime.watch_jobs(load_jobs)

        ligands = runtime.molecules.select(role="ligand")
        receptors = runtime.molecules.select(role="receptor")

        descriptor_job = runtime.qsar.compute_descriptors(molecule_set=ligands, executor_name="process")
        runtime.watch_job(descriptor_job)

        receptor = receptors.one()
        runtime.docking.set_grid(
            receptor_id=int(receptor.id or 0),
            center=box_center,
            size=box_size,
        )

        prep_jobs = [
            runtime.docking.prepare_ligands(ligands=ligands, executor_name="process"),
            runtime.docking.prepare_receptors(receptors=receptors, executor_name="process"),
        ]
        runtime.watch_jobs(prep_jobs)

        docking_job = runtime.docking.run(
            ligands=ligands,
            receptors=receptors,
            executor_name="process",
            vina_cpu=1,
            box_center=box_center,
            box_size=box_size,
        )
        runtime.watch_job(docking_job)

        print(f"Docking results: {len(runtime.docking.list_results())}")
    finally:
        runtime.shutdown()


if __name__ == "__main__":
    main()
