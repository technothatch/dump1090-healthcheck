.DEFAULT_GOAL := help
.PHONY: help install lint format test check clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install package with dev extras
	python -m pip install -e ".[dev]"

lint: ## Run ruff check, ruff format check and mypy
	ruff check .
	ruff format --check .
	mypy src

format: ## Apply ruff fixes and formatting
	ruff check --fix .
	ruff format .

test: ## Run tests with coverage
	python -m pytest -q --cov --cov-report=term-missing --cov-fail-under=80

check: lint test ## Full local gate (what CI runs)

clean: ## Remove build and cache artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist build
	rm -rf src/*.egg-info
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
