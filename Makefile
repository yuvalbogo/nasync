.PHONY: help install install-dev test clean format lint

help:
	@echo "NASync - Development Commands"
	@echo ""
	@echo "  make install       Install the package"
	@echo "  make install-dev   Install with dev dependencies"
	@echo "  make test          Run tests"
	@echo "  make clean         Clean build artifacts"
	@echo "  make format        Format code with black"
	@echo "  make lint          Check code with flake8"

install:
	pip install -e .

install-dev:
	pip install -e ".[dev]"

test:
	python -m pytest tests/ -v

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf build/ dist/ *.egg-info/

format:
	black nasync/ tests/ --line-length 100

lint:
	flake8 nasync/ tests/ --max-line-length 100
