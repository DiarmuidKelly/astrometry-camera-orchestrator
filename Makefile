VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: help
help:
	@echo "camera-orchestrator"
	@echo ""
	@echo "  make venv                    create the virtualenv"
	@echo "  make install                 install runtime deps"
	@echo "  make batch FOLDER=<path>     plate-solve all images in a folder"
	@echo "  make batch-fast FOLDER=<path> fast solve (lower accuracy)"
	@echo "  make test                    run unit tests"
	@echo "  make clean                   remove venv and build artefacts"

$(VENV):
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip

.PHONY: venv
venv: $(VENV)

.PHONY: install
install: $(VENV)
	$(PIP) install -r requirements.txt

FOLDER ?= ./incoming

.PHONY: batch
batch:
	$(PY) main.py batch $(FOLDER) --config config.yaml --annotate

.PHONY: batch-fast
batch-fast:
	$(PY) main.py batch $(FOLDER) --config config.yaml --annotate --mode fast

.PHONY: test
test:
	$(VENV)/bin/pytest tests/ -v

.PHONY: clean
clean:
	rm -rf $(VENV) **/__pycache__ *.egg-info
