VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

# Where the raw grab test writes its frame.
FRAME ?= /tmp/m50_test.jpg

.PHONY: help
help:
	@echo "camera-orchestrator"
	@echo ""
	@echo "  make venv            create the virtualenv ($(VENV))"
	@echo "  make install         install runtime deps into the venv"
	@echo "  make batch FOLDER=.. batch plate-solve all images in a folder"
	@echo "  make watch           watch incoming/ folder and solve new files live"
	@echo "  make clean           remove the venv and build artefacts"

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
	$(PY) scripts/batch_solve.py $(FOLDER) --config config.yaml

.PHONY: watch
watch:
	$(PY) scripts/watch_and_solve.py --config config.yaml

.PHONY: clean
clean:
	rm -rf $(VENV) **/__pycache__ *.egg-info
