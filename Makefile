# AgentVoice: AI phone sales agent (Linux server + dashboard, Android control app)
#
#   make install          create .venv and install the server
#   make run              run the server + dashboard on http://localhost:8080
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

.PHONY: install run token test install-service uninstall-service install-desktop compile_apk release_apk clean

$(BIN)/agentvoice: server/pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -e "server[dev,local]"
	@touch $@

install: $(BIN)/agentvoice $(ENV_FILE)
	@echo "Installed. Edit $(ENV_FILE), then: make run"

$(ENV_FILE):
	cp .env.example $(ENV_FILE)
	@chmod 600 $(ENV_FILE)
	@echo "Created $(ENV_FILE) from .env.example"

run: install
	$(BIN)/agentvoice --env $(ENV_FILE) serve

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
