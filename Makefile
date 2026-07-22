# Dev tooling only (setup, lint, test) — powered by uv. The app is a Python CLI:
#   uv run camera-orchestrator --help     (or: uv run python main.py --help)

.PHONY: help
help:
	@echo "camera-orchestrator — dev tooling (uv)"
	@echo ""
	@echo "  make install                 sync runtime deps (uv sync --no-dev)"
	@echo "  make install-dev             sync runtime + dev deps (uv sync)"
	@echo "  make test                    run unit tests"
	@echo "  make test-integration        integration tests (needs Docker + indexes)"
	@echo "  make lint                    ruff + mypy"
	@echo "  make fmt                     ruff format"
	@echo "  make clean                   remove venv and build artefacts"
	@echo ""
	@echo "  app usage: uv run camera-orchestrator --help"

.PHONY: install
install:
	uv sync --no-dev

.PHONY: install-dev
install-dev:
	uv sync

.PHONY: lint
lint:
	uv run ruff check .
	uv run mypy camera_orchestrator/ main.py --ignore-missing-imports

.PHONY: fmt
fmt:
	uv run ruff format .

.PHONY: test
test:
	uv run pytest tests/ -v --ignore=tests/test_integration_solver.py --cov=camera_orchestrator

.PHONY: setup-integration
setup-integration:
	docker pull diarmuidk/astrometry-dockerised-solver:latest

.PHONY: test-integration
test-integration: setup-integration
	uv run pytest tests/test_integration_solver.py -v --cov=camera_orchestrator

.PHONY: test-all
test-all:
	uv run pytest tests/ -v --cov=camera_orchestrator

.PHONY: clean
clean:
	rm -rf .venv **/__pycache__ *.egg-info
