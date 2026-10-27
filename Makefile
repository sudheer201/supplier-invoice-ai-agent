.PHONY: install samples run test eval docker

install:
	pip install -r requirements-dev.txt

samples:
	python -m scripts.generate_invoices

run:
	uvicorn app.api:app --reload --port 8000

test:
	pytest -q

eval:
	python -m eval.run_eval

docker:
	docker compose up --build
