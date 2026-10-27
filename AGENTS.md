## Dev environment tips
- Use `MSDD_312` conda environment to run any command with python.
- Answers to questions related to functionalities should be checked against the code before responding.
- AMDock (or AMDockVS, it's the same thing) uses Molsuite (orchestrator at ../molsuite_flow, Qt UI widgets at ../molsuite_components) as its task orchestrator, executors, and data flow manager. Anything related to this must use the Molsuite APIs.
- AMDock is chemical-based and uses multiple dependencies to resolve tasks.