# Local targets need the venv active (pip install -e ".[dev]"); docker-* targets go through compose.
.PHONY: run test lint docker-build docker-up docker-chaos docker-test docker-down

run:
	uvicorn app.main:app --reload --port 8000

test:
	pytest -q

lint:
	ruff check app tests

docker-build:
	docker compose build

docker-up:
	docker compose up --build

docker-chaos:
	docker compose --profile chaos up --build

docker-test:
	docker compose --profile test run --rm --build tests

docker-down:
	docker compose --profile chaos --profile test down --remove-orphans
