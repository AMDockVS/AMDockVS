---
icon: amdockvs/pymol
---

# <span class="tool-heading-icon tool-heading-icon--pymol" aria-hidden="true"></span> PyMOL

**PyMOL** is the embedded 3D viewer on the right side of the workspace. It
shows receptor structures, ligands, docking poses, and binding-site boxes.
Open it from the right tool rail, then select a molecule or docking pose in
the central catalog to inspect it.

![A docking pose selected in Docking Results and displayed in the embedded PyMOL viewer](images/AMDockVS_docking_studio_results.png)

## 1. Choose what to display

The viewer follows the structure selected in the active catalog or result
view. Select a receptor to inspect its structure and active binding site,
a ligand to inspect that molecule, or a pose in **Docking Results** to see
the receptor–ligand complex.

In [Pocket Detection](pocket_detection.md), selecting predicted sites displays
their pocket points and centers on the receptor. The available display
presets change with the kind of scene being shown.

PyMOL displays the coordinates stored for the selected molecule. To generate
a 3D structure or change its preparation, use [Build](build.md) or
[Docking Studio](docking_studio.md).

For an ESMFold prediction, open the bottom **Panel** and choose **Color by
pLDDT**. The prediction and confidence data remain saved when you reopen the
project. After structure preparation, **Color prediction by pLDDT** displays
the saved prediction with its original confidence values; **Show Current**
returns to the prepared working structure.

## 2. Use the viewer toolbar

The toolbar provides the following actions. In a narrow panel, some actions
move into its overflow menu.

- <span class="tool-list-icon tool-list-icon--pymol-selection" aria-hidden="true"></span> **Selection** — Choose whether mouse picks select atoms, residues, chains, or objects. The menu also clears the PyMOL selection.
- <span class="tool-list-icon tool-list-icon--pymol-actions" aria-hidden="true"></span> **Actions** — Use **Zoom**, **Orient**, or **Center** on the current PyMOL selection or scene target.
- <span class="tool-list-icon tool-list-icon--pymol-display" aria-hidden="true"></span> **Display** — Apply Cartoon, Sticks, Lines, Surface, Spheres, Ribbon, or Dots. In comparison scenes, the menu also offers Grid and Overlay layouts.
- <span class="tool-list-icon tool-list-icon--pymol-hide" aria-hidden="true"></span> **Hide** — Hide a representation or everything for the current selection or scene target.
- <span class="tool-list-icon tool-list-icon--pymol-hydrogens" aria-hidden="true"></span> **Hydrogens** — Show polar hydrogens (bonded to N, O, or S) by default. Click to cycle through hidden, all, and polar hydrogens in the PyMOL session.
- <span class="tool-list-icon tool-list-icon--pymol-presets" aria-hidden="true"></span> **Presets** — Apply a display preset compatible with the current scene.

The atom or residue selection made inside PyMOL controls viewer actions. The
global molecule selection used by Build and Docking Studio is made separately
in the catalog tables.

## 3. Choose a display preset

Open **Presets** in the PyMOL toolbar. The available styles depend on the
current scene. Click an image to enlarge it. The views use the
[ABL–imatinib crystal structure (PDB 1IEP)](https://www.rcsb.org/structure/1IEP).

=== "Structures"

    <div class="pymol-preset-gallery" markdown>

    - ![Blue cartoon of the ABL kinase fold](images/pymol-presets/amdock-receptor.webp){ loading=lazy width=900 height=640 }

        **AMDock · Receptor**

        Inspect the receptor fold as a cartoon.

    - ![Orange imatinib sticks with nitrogen atoms in blue and oxygen in red](images/pymol-presets/amdock-ligand.webp){ loading=lazy width=900 height=640 }

        **AMDock · Ligand**

        Inspect the ligand on its own, centered and shown as sticks.

    - ![Orange imatinib sticks in the blue ABL receptor cartoon](images/pymol-presets/amdock-protein-ligand.webp){ loading=lazy width=900 height=640 }

        **AMDock · Protein-ligand**

        Locate the ligand or docking pose in the receptor fold.

    </div>

=== "Pockets"

    Select a site in [Pocket Detection](pocket_detection.md) to use these presets.

    <div class="pymol-preset-gallery" markdown>

    - ![A cluster of gold P2Rank points marking a pocket on the receptor cartoon](images/pymol-presets/binding-points.webp){ loading=lazy width=900 height=640 }

        **Binding sites · Points**

        Locate the predicted pocket and its center.

    - ![Predicted pocket points with the surrounding receptor residues shown as sticks](images/pymol-presets/binding-residues.webp){ loading=lazy width=900 height=640 }

        **Binding sites · Residues**

        Inspect receptor residues within 4 Å of the pocket points.

    - ![Predicted pocket points and residue sticks under a translucent local surface](images/pymol-presets/binding-surface.webp){ loading=lazy width=900 height=640 }

        **Binding sites · Surface**

        See the local surface around the predicted pocket.

    </div>

=== "Search space"

    Show a box in **Grid Box** to highlight the receptor residues that intersect it.

    <div class="pymol-preset-gallery" markdown>

    - ![A patch of pink residues on the neutral receptor surface](images/pymol-presets/surface-highlight.webp){ loading=lazy width=900 height=640 }

        **Surface · Site highlight**

        Locate the search-space region as a pink patch on the receptor surface.

    - ![The same receptor surface patch colored by residue chemistry](images/pymol-presets/surface-nature.webp){ loading=lazy width=900 height=640 }

        **Surface · Residue nature**

        Distinguish acidic, basic, polar, aromatic, and hydrophobic residues in the region.

    </div>

=== "PyMOL"

    <div class="pymol-preset-gallery" markdown>

    - ![The receptor and ligand drawn as thin atom-colored bond lines](images/pymol-presets/pymol-default.webp){ loading=lazy width=900 height=640 }

        **PyMOL · Default**

        Show bonds as lines and unbonded atoms as crosses.

    - ![A receptor ribbon, ligand sticks, and red solvent markers](images/pymol-presets/pymol-simple.webp){ loading=lazy width=900 height=640 }

        **PyMOL · Simple**

        Show the receptor as a ribbon with ligand sticks and solvent markers.

    - ![A receptor ribbon and ligand sticks with solvent markers hidden](images/pymol-presets/pymol-simple-no-solvent.webp){ loading=lazy width=900 height=640 }

        **PyMOL · Simple (no solvent)**

        Use the Simple view with solvent hidden.

    - ![Small spheres at atom positions connected by sticks](images/pymol-presets/pymol-ball-and-stick.webp){ loading=lazy width=900 height=640 }

        **PyMOL · Ball and stick**

        Show atom positions as small spheres and bonds as sticks.

    - ![A rainbow receptor cartoon with the ligand shown as sticks](images/pymol-presets/pymol-pretty.webp){ loading=lazy width=900 height=640 }

        **PyMOL · Pretty**

        Follow the protein chain in a rainbow cartoon.

    - ![A rainbow receptor cartoon and ligand sticks with red water atoms visible](images/pymol-presets/pymol-pretty-solvent.webp){ loading=lazy width=900 height=640 }

        **PyMOL · Pretty (solvent)**

        Use the Pretty view with water molecules visible.

    - ![A rainbow protein cartoon with smoothed loops and detailed helices](images/pymol-presets/pymol-publication.webp){ loading=lazy width=900 height=640 }

        **PyMOL · Publication**

        Use a smoothed cartoon with more detailed helices.

    - ![A tube tracing the protein backbone with varying thickness and color](images/pymol-presets/pymol-b-factor-putty.webp){ loading=lazy width=900 height=640 }

        **PyMOL · B-factor Putty**

        Map the stored B-factors to the thickness and color of the backbone.

    - ![Ligand sticks, receptor bond lines, and dashed polar contacts in the pocket](images/pymol-presets/pymol-technical.webp){ loading=lazy width=900 height=640 }

        **PyMOL · Technical**

        Inspect bonds and dashed polar contacts.

    - ![The ligand and nearby residues against a receptor cartoon with dashed polar contacts](images/pymol-presets/pymol-ligand-sites-cartoon.webp){ loading=lazy width=900 height=640 }

        **PyMOL · Ligand Sites (cartoon)**

        Inspect the ligand and nearby residues in cartoon context.

    - ![Ligand sticks in a pocket bordered by patches of receptor surface](images/pymol-presets/pymol-ligand-sites-surface.webp){ loading=lazy width=900 height=640 }

        **PyMOL · Ligand Sites (surface)**

        See the shape of the receptor pocket around the ligand sticks.

    </div>

## 4. Inspect the search space

**Grid Box** opens the side panel beside the 3D canvas. Select a receptor
to review its sites and the center and dimensions of its working box. The
panel's save and activation actions control which site is used for docking.

![Docking Studio with the receptor and Grid Box side panel visible in PyMOL](images/AMDockVS_docking_studio_receptors.png)

Use [Pocket Detection](pocket_detection.md) to predict candidate sites, or
review the receptor's active box during the
[Receptors step of Docking Studio](docking_studio.md#receptors).

## 5. Adjust the scene and export

**Scene** provides additional scene, camera, and rendering options.
**Color by Role** distinguishes receptor and ligand atoms while keeping
heteroatoms colored by element. **Clear Scene** removes the displayed objects
from PyMOL; it does not delete their project records. The command line beneath
the canvas accepts PyMOL commands for further adjustments.

**Export** offers **Save displayed complex as PDB...** and
**Save ray-traced PNG...**. The image export uses a print-sized rendering at
300 dpi.

For a complementary view of the selected pose's contacts, open
[2D Interactions](interactions_2d.md).
