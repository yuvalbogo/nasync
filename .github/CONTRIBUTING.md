# Contributing to SAMBA Sync Auto

Thank you for your interest in contributing! Here's how you can help.

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YuvalBogo/nasync.git`
3. Create a virtual environment: `python -m venv venv && source venv/bin/activate`
4. Install in development mode: `pip install -e .`
5. Install the samba-client system package if you haven't already

## Making Changes

- Keep changes focused and atomic
- Test your changes locally: `python -m samba_sync.test_config`
- Follow existing code style
- Update README if adding features

## Submitting Changes

1. Create a feature branch: `git checkout -b feature/my-feature`
2. Commit your changes with clear messages
3. Push to your fork
4. Open a pull request with a description of your changes

## Reporting Issues

- Check existing issues first
- Include your OS, Python version, and SAMBA server details
- Describe steps to reproduce
- Share relevant error messages

## Areas for Contribution

- Better error handling and recovery
- Performance optimizations
- Additional sync strategies (delta sync, dry-run)
- GUI implementation
- Tests and documentation
- Cross-platform testing

Thank you!
