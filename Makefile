.PHONY: help install install-dev install-user test lab clean format lint

help:
	@echo "NASync - Development Commands"
	@echo ""
	@echo "  make install       Install the package"
	@echo "  make install-dev   Install with dev dependencies"
	@echo "  make install-user  Install for everyday use, apart from this checkout (REF=main)"
	@echo "  make test          Run tests"
	@echo "  make lab           Run the data-safety lab (real Samba, needs podman)"
	@echo "  make clean         Clean build artifacts"
	@echo "  make format        Format code with black"
	@echo "  make lint          Check code with flake8"

install:
	pip install -e .

install-dev:
	pip install -e ".[dev]"

# The app launcher and background service run this install, not the checkout,
# so switching branches here never changes what syncs tonight.
REF ?= main
install-user:
	sh scripts/install-user $(REF)

test:
	python -m pytest tests/ -v

# The data-safety lab: NASync against a throwaway Samba container. Run it
# before every release and after any change to how files are compared,
# copied, renamed or deleted. See DATA_SAFETY.md.
lab:
	python -m pytest tests/lab --lab -v

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf build/ dist/ *.egg-info/

format:
	black nasync/ tests/ --line-length 100

lint:
	flake8 nasync/ tests/ --max-line-length 100
