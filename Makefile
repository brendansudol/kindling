.PHONY: lint lint-fix format format-check test check

lint:
	.venv/bin/ruff check scripts tests

lint-fix:
	.venv/bin/ruff check --fix scripts tests

format:
	.venv/bin/ruff format scripts tests

format-check:
	.venv/bin/ruff format --check scripts tests

test:
	.venv/bin/python -m unittest discover -s tests

check: lint format-check test
