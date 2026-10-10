PY ?= python3
VENV := .venv
BIN := $(VENV)/bin
PORT ?= 47821

.DEFAULT_GOAL := help
.PHONY: help install demo serve evals ask tools egress audit test cover lint fmt clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

$(BIN)/payer-agents: pyproject.toml
	@command -v uv >/dev/null 2>&1 \
	  && uv venv --python 3.12 $(VENV) && uv pip install -e ".[dev]" \
	  || ( $(PY) -m venv $(VENV) && $(BIN)/pip install -q --upgrade pip && \
	       $(BIN)/pip install -q -e ".[dev]" )
	@touch $(BIN)/payer-agents

install: $(BIN)/payer-agents ## Create the virtualenv and install the project

demo: install ## Run the scripted walkthrough (no Azure, no network)
	$(BIN)/payer-agents demo

serve: install ## Start the demo console on http://127.0.0.1:$(PORT)
	$(BIN)/payer-agents serve --port $(PORT)

evals: install ## Score the labelled synthetic cases
	$(BIN)/payer-agents evals

ask: install ## make ask Q="does my plan cover hearing aids?"
	$(BIN)/payer-agents ask "$(Q)"

tools: install ## List MCP tools and their gateway status
	$(BIN)/payer-agents tools

egress: install ## Evaluate sample destinations against the egress policy
	$(BIN)/payer-agents egress \
	  https://apim-payer-agents.azure-api.net/mcp \
	  https://myaccount.fhir.azurehealthcareapis.com/Patient \
	  https://api.openai.com/v1/chat/completions

audit: install ## Run the demo and print the hash-chained audit trail
	$(BIN)/payer-agents audit

test: install ## Run the test suite with coverage gates
	$(BIN)/pytest

cover: install ## Write an HTML coverage report to htmlcov/
	$(BIN)/pytest --cov-report=html
	@echo "open htmlcov/index.html"

lint: install ## Lint and format-check
	$(BIN)/ruff check src tests
	$(BIN)/ruff format --check src tests

fmt: install ## Apply formatting
	$(BIN)/ruff format src tests
	$(BIN)/ruff check --fix src tests

clean: ## Remove build and cache artefacts
	rm -rf $(VENV) .pytest_cache .ruff_cache .coverage htmlcov out
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
