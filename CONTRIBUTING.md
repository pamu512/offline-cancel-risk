# Contributing

## Development setup

1. Clone the repository and create a virtual environment:

   ```bash
   python -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   ```

2. Install the package in editable mode with dev dependencies:

   ```bash
   pip install -e ".[dev]"
   ```

3. Run the test suite from the repository root:

   ```bash
   pytest
   ```

## Pull requests

- Keep changes focused; one logical change per PR when possible.
- Add or update tests for behavior you change.
- Ensure `pytest` passes before requesting review.
- Follow existing code style and naming in touched files.
- Describe what changed and why in the PR summary.

## License

By contributing, you agree that your contributions will be licensed under the Apache License 2.0.
