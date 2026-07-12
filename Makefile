VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: help
help:
	@echo "camera-orchestrator"
	@echo ""
	@echo "  make batch FOLDER=<path> [CONFIG=config.yaml]       plate-solve all images in a folder"
	@echo "  make batch-fast FOLDER=<path> [CONFIG=config.yaml]  fast solve (lower accuracy)"
	@echo "  make grab [CONFIG=config.yaml] [OUT=<path>]         download latest image from camera"
	@echo "  make grab POLL=5 [CONFIG=config.yaml] [OUT=<path>]  poll camera every N seconds"
	@echo ""
	@echo "  make install                 install runtime deps"
	@echo "  make install-dev             install runtime + dev deps"
	@echo "  make test                    run unit tests"
	@echo "  make lint                    ruff + mypy"
	@echo "  make fmt                     ruff format"
	@echo "  make clean                   remove venv and build artefacts"

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

FOLDER ?= ./incoming
CONFIG ?= config.yaml

.PHONY: batch
batch:
	$(PY) main.py --config $(CONFIG) batch $(FOLDER) --annotate

.PHONY: batch-fast
batch-fast:
	$(PY) main.py --config $(CONFIG) batch $(FOLDER) --annotate --mode fast

OUT ?=
POLL ?=

.PHONY: grab
grab:
	$(PY) main.py --config $(CONFIG) grab $(if $(OUT),--out $(OUT)) $(if $(POLL),--poll $(POLL))

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
