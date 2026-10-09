.PHONY: install run test lint format samples docker docker-run

install:
	pip install -e ".[dev]"

run:
	uvicorn app.main:app --reload

test:
	pytest --cov=app --cov-report=term-missing

lint:
	ruff check .
	ruff format --check .
	mypy app

format:
	ruff format .
	ruff check . --fix

samples:
	python scripts/make_samples.py

docker:
	docker build -t geomeasure .

docker-run:
	docker compose up --build
