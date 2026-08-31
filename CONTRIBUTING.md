# Contributing to YANG-Evolve

Thank you for your interest in contributing to YANG-Evolve. We welcome focused, high-quality contributions that improve compatibility analysis, documentation, testing, or developer experience.

## General Guidelines

- Keep contributions focused and reasonably small; smaller changes are easier to review and integrate.
- Prioritize clarity, correctness, and maintainability in both code and documentation.
- Changes addressing existing issues will generally receive review priority.
- Before starting significant work, consider opening an issue to discuss the change and confirm scope.

## Code of Conduct

Please be respectful, constructive, and collaborative in issues, pull requests, and discussions. We aim to keep the project welcoming and productive for everyone involved.

## Getting Started

1. Fork the repository.
2. Clone your fork locally.
3. Create a new branch for your changes.
4. Implement and test your changes.
5. Submit a pull request describing the change and its motivation.

## Development Workflow

### 1. Prepare the environment

Create a virtual environment and install the project in editable mode if needed:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

### 2. Submit a pull request

When opening a pull request, include:

- a clear summary of the change
- the reason or problem it addresses
- any relevant testing performed
- screenshots or sample output when helpful

## Scope of Contributions

We welcome contributions in areas such as:

- YANG compatibility rule improvements
- bug fixes and edge-case handling
- documentation and examples
- test coverage for compatibility scenarios
- CLI or plugin usability improvements
- performance and reliability enhancements

## Review Process

Pull requests are reviewed based on correctness, scope, clarity, and alignment with project goals. Maintainers may request revisions before merging.

## License

By contributing to this project, you agree that your contributions will be licensed under the Apache License 2.0.
