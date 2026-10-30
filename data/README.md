`data/` is a local workspace, not an official product interface.

It holds large datasets, temporary projects, and test outputs that support local
development but should not define the repository's primary narrative.

Policy:

- Do not commit large datasets or work projects to Git.
- Keep only `data/README.md` and `data/fixtures/` in the repository.
- Use `data/fixtures/` for small, controlled samples that need to be versioned.
- Treat folders such as `project_*`, `pdemo*`, and large datasets as disposable local material.
- If an official example needs data, use small fixtures or generate the data temporarily.
