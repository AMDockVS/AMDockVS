from pathlib import Path

from amdockvs import AMDockVSRuntime


def main():
    project_dir = Path("./quickstart_project").resolve()
    ligands_file = Path("./inputs/ligands.sdf").resolve()
    receptor_file = Path("./inputs/receptor.pdb").resolve()
    box_center = (12.0, 13.0, 10.0)
    box_size = (20.0, 20.0, 20.0)

    runtime = AMDockVSRuntime()
    try:
        runtime.create_or_open_project(name="quickstart", folder=project_dir)

        import_jobs = [
            *runtime.loader.load_ligands([ligands_file], executor_name="thread"),
            *runtime.loader.load_receptors([receptor_file], executor_name="thread"),
        ]
        runtime.watch_jobs(import_jobs)

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

        ligand_prep_job = runtime.docking.prepare_ligands(ligands=ligands, executor_name="process")
        receptor_prep_job = runtime.docking.prepare_receptors(receptors=receptors, executor_name="process")
        runtime.watch_jobs([ligand_prep_job, receptor_prep_job])

        docking_job = runtime.docking.run(
            ligands=ligands,
            receptors=receptors,
            executor_name="process",
            vina_cpu=1,
            box_center=box_center,
            box_size=box_size,
        )
        runtime.watch_job(docking_job)

        stats = runtime.docking.result_stats()
        print(f"Results: {stats.total_results}, completed: {stats.completed_results}")
    finally:
        runtime.shutdown()


if __name__ == "__main__":
    main()
