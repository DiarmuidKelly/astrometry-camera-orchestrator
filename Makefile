VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

# Dev tooling only (setup, lint, test). The app is a Python CLI — for usage run:
#   python main.py --help     (or ./main.py --help, python -m camera_orchestrator --help)

.PHONY: help
help:
	@echo "camera-orchestrator — dev tooling"
	@echo ""
	@echo "  make install                 install runtime deps"
	@echo "  make install-dev             install runtime + dev deps"
	@echo "  make test                    run unit tests"
	@echo "  make test-integration        integration tests (needs Docker + indexes)"
	@echo "  make lint                    ruff + mypy"
	@echo "  make fmt                     ruff format"
	@echo "  make clean                   remove venv and build artefacts"
	@echo ""
	@echo "  app usage: python main.py --help"

$(VENV):
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip

.PHONY: venv
venv: $(VENV)

.PHONY: install
install: $(VENV)
	$(PIP) install -r requirements.txt

.PHONY: install-dev
install-dev: $(VENV)
	$(PIP) install -r requirements-dev.txt

.PHONY: lint
lint:
	$(VENV)/bin/ruff check .
	$(VENV)/bin/mypy camera_orchestrator/ main.py --ignore-missing-imports

.PHONY: fmt
fmt:
	$(VENV)/bin/ruff format .

.PHONY: test
test:
	$(VENV)/bin/pytest tests/ -v --ignore=tests/test_integration_solver.py --cov=camera_orchestrator

.PHONY: setup-integration
setup-integration:
	docker pull diarmuidk/astrometry-dockerised-solver:latest

.PHONY: test-integration
test-integration: setup-integration
	$(VENV)/bin/pytest tests/test_integration_solver.py -v --cov=camera_orchestrator

.PHONY: test-all
test-all:
	$(VENV)/bin/pytest tests/ -v --cov=camera_orchestrator

.PHONY: clean
clean:
	rm -rf $(VENV) **/__pycache__ *.egg-info
