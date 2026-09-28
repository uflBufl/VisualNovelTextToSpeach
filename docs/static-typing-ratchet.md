# Full-package static typing

`uv run --frozen mypy` checks every production module under `vntts`. CI runs
the same command with the locked development dependencies, so local and CI
typing results use one scope.
