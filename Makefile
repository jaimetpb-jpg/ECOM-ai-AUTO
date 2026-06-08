.PHONY: help install test test-v51 start stop logs shell migrate oracle monitor orchestrator

help:
	@echo "AI Ecommerce System V5.1 — Commands"
	@echo ""
	@echo "  make install       Install Python dependencies"
	@echo "  make migrate       Run DB schema migrations"
	@echo "  make test          Run full V5.1 test suite"
	@echo "  make start         Start all services (Docker)"
	@echo "  make stop          Stop all services"
	@echo "  make logs          Tail API logs"
	@echo "  make shell         Open API container shell"
	@echo "  make oracle        Trigger Oracle cycle manually"
	@echo "  make monitor       Trigger monitoring cycle manually"
	@echo "  make orchestrator  Trigger V5.1 autonomous cycle"

install:
	python3.11 -m venv venv
	. venv/bin/activate && pip install -r requirements.txt
	@echo "✅ Dependencies installed. Activate with: source venv/bin/activate"

migrate:
	python -c "from shared.supabase_client import SupabaseClient; SupabaseClient().run_migrations(); print('✅ Schema V5.1 applied')"

test:
	python scripts/run_v51_tests.py

start:
	docker-compose -f infra/docker-compose.yml up -d
	@echo "✅ Services started"
	@echo "   API:      http://localhost:8000/docs"
	@echo "   n8n:      http://localhost:5678"
	@echo "   Metabase: http://localhost:3000"

stop:
	docker-compose -f infra/docker-compose.yml down

logs:
	docker-compose -f infra/docker-compose.yml logs -f api

shell:
	docker-compose -f infra/docker-compose.yml exec api bash

dev:
	uvicorn main:app --reload --port 8000

oracle:
	curl -s -X POST http://localhost:8000/api/oracle/run \
		-H "X-API-Key: $$(grep API_KEY .env | cut -d= -f2)" \
		-H "Content-Type: application/json" \
		-d '{"tenant_id":"default"}' | python -m json.tool

monitor:
	curl -s -X POST http://localhost:8000/api/monitoring/run \
		-H "X-API-Key: $$(grep API_KEY .env | cut -d= -f2)" \
		-H "Content-Type: application/json" \
		-d '{"tenant_id":"default"}' | python -m json.tool

orchestrator:
	curl -s -X POST http://localhost:8000/api/v51/orchestrator/run \
		-H "X-API-Key: $$(grep API_KEY .env | cut -d= -f2)" \
		-H "Content-Type: application/json" \
		-d '{"tenant_id":"default","niches":["masajeadores cervicales","vitaminas cabello"],"total_budget_usd":800}' | python -m json.tool
