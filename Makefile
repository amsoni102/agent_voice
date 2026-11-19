# AgentVoice: AI phone sales agent (Linux server + dashboard, Android control app)
#
#   make install          create .venv and install the server
#   make run              run the server + dashboard on http://localhost:8080
#   make voice            download the Kokoro voice model (done by make install)
#   make stop             stop the server (ports from .env, plus any leftover agentvoice)
#   make token            print the API token for the dashboard / Android app
#   make test             run the server test suite
#   make install-service  run in the background via a systemd user service (starts on login)
#   make install-desktop  add an "AgentVoice" launcher to your applications menu
#   make compile_apk      build the Android app -> android/agentvoice-debug.apk
#   make release_apk      build a signed release APK (needs android/keystore.properties)

PYTHON ?= python3
VENV := $(CURDIR)/.venv
BIN := $(VENV)/bin
ENV_FILE := $(CURDIR)/.env
# Defaults match .env.example; overridden below when .env exists.
PORT ?= 8080
HTTPS_PORT ?= 8443
KOKORO_DIR := $(HOME)/.local/share/agentvoice/kokoro
KOKORO_URL := https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0

.PHONY: install voice run stop token test install-service uninstall-service install-desktop compile_apk release_apk clean

ifneq (,$(wildcard $(ENV_FILE)))
  PORT := $(shell awk -F= '/^PORT=/{gsub(/\r/,""); print $$2; exit}' $(ENV_FILE))
  HTTPS_PORT := $(shell awk -F= '/^HTTPS_PORT=/{gsub(/\r/,""); print $$2; exit}' $(ENV_FILE))
  ifeq ($(PORT),)
    PORT := 8080
  endif
  ifeq ($(HTTPS_PORT),)
    HTTPS_PORT := 8443
  endif
endif

$(BIN)/agentvoice: server/pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -e "server[dev,local]"
	@touch $@

install: $(BIN)/agentvoice $(ENV_FILE) voice
	@echo "Installed. Edit $(ENV_FILE), then: make run"

voice: $(KOKORO_DIR)/kokoro-v1.0.onnx $(KOKORO_DIR)/voices-v1.0.bin

$(KOKORO_DIR)/%:
	@mkdir -p $(KOKORO_DIR)
	curl -fL --progress-bar -o $@.part $(KOKORO_URL)/$* && mv $@.part $@

$(ENV_FILE):
	cp .env.example $(ENV_FILE)
	@chmod 600 $(ENV_FILE)
	@echo "Created $(ENV_FILE) from .env.example"

run: install
	$(BIN)/agentvoice --env $(ENV_FILE) serve

stop:
	@# Stop systemd user service if this machine uses it.
	@systemctl --user stop agentvoice.service 2>/dev/null || true
	@# Free HTTP/HTTPS listeners (make run / background leftovers).
	@fuser -k $(PORT)/tcp >/dev/null 2>&1 || true
	@if [ -n "$(HTTPS_PORT)" ] && [ "$(HTTPS_PORT)" != "0" ]; then \
		fuser -k $(HTTPS_PORT)/tcp >/dev/null 2>&1 || true; \
	fi
	@# Match the real binary only — a broad pkill pattern can kill this recipe's shell.
	@pkill -f '[.]venv/bin/agentvoice .*serve' 2>/dev/null || true
	@sleep 0.3
	@if ss -ltn 2>/dev/null | grep -qE ":$(PORT)[[:space:]]|:$(HTTPS_PORT)[[:space:]]"; then \
		echo "Something is still listening on $(PORT)/$(HTTPS_PORT)."; \
		ss -ltnp 2>/dev/null | grep -E ":$(PORT)[[:space:]]|:$(HTTPS_PORT)[[:space:]]" || true; \
		exit 1; \
	fi
	@echo "AgentVoice stopped (ports $(PORT) / $(HTTPS_PORT))."

token: install
	@$(BIN)/agentvoice --env $(ENV_FILE) token

test: $(BIN)/agentvoice
	cd server && $(BIN)/python -m pytest -q

install-service: install
	mkdir -p $(HOME)/.config/systemd/user
	sed -e "s|@ROOT@|$(CURDIR)|g" deploy/agentvoice.service > $(HOME)/.config/systemd/user/agentvoice.service
	systemctl --user daemon-reload
	systemctl --user enable --now agentvoice.service
	@echo "Running. Logs: journalctl --user -u agentvoice -f"

uninstall-service:
	-systemctl --user disable --now agentvoice.service
	rm -f $(HOME)/.config/systemd/user/agentvoice.service
	systemctl --user daemon-reload

install-desktop: install
	mkdir -p $(HOME)/.local/share/applications
	chmod +x deploy/open-dashboard.sh
	sed -e "s|@ROOT@|$(CURDIR)|g" deploy/agentvoice.desktop > $(HOME)/.local/share/applications/agentvoice.desktop
	@echo "Added AgentVoice to your applications menu"

compile_apk:
	$(MAKE) -C android compile_apk

release_apk:
	$(MAKE) -C android release_apk

clean:
	rm -rf $(VENV) server/*.egg-info server/.pytest_cache
	-$(MAKE) -C android clean
