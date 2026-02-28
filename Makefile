.PHONY: lint lint-fix format format-check check

lint:
	.venv/bin/ruff check scripts

lint-fix:
	.venv/bin/ruff check --fix scripts

format:
	.venv/bin/ruff format scripts

format-check:
	.venv/bin/ruff format --check scripts

check: lint format-check
