# Dependency baseline

`requirements.txt` pins direct package versions used to prepare this source-only
release. It is not a hash-locked dependency closure and does not include Python,
system libraries, model files, GPU drivers or every transitive package.

Install the matched CPU PyTorch / torchvision pair using the official CPU wheel
index shown in the README before resolving the remaining requirements. For other
hardware, use [PyTorch's official instructions](https://pytorch.org/get-started/locally/)
and revalidate the pair. Linux VTK wheels may need distribution packages such as
`libgl1`, `libglib2.0-0`, `libxrender1` and `libxext6`.

The adapter in `bounded_inference.py` rewrites selected upstream Python functions
in worker memory to avoid unnecessary copies. It does not modify installed
packages. Unknown source shapes fail closed. `worker.py` relies on task mappings
and internal configuration tables; upgrading TotalSegmentator or nnU-Net requires
explicit adapter and model-plan verification, not only changing version numbers.

The total task uses localizer task 298 and selected high-resolution task groups
291–295. The lung-vessel plan depends on a separate upstream task. Exact weight
folder names and task requirements come from the installed upstream package;
missing checkpoints stop inference. No bundled model registry or license exists.

Validate on the intended platform, then produce a local full freeze / lock file,
retain wheel hashes and inventory weights separately. Do not publish a freeze
that contains private indexes, embedded authentication or local user paths.
Run `python -m pip check` and the synthetic tests after each dependency change.
