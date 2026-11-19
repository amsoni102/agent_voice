# Build the AgentVoice Android control app.
#   make compile_apk     debug APK  -> ./agentvoice-debug.apk
#   make release_apk     signed release APK (needs keystore.properties) -> ./agentvoice.apk
#   make install         install the debug APK on a connected device (adb)

JAVA_HOME ?= /usr/lib/jvm/java-21-openjdk-amd64
export JAVA_HOME

APK_SRC := app/build/outputs/apk/debug/app-debug.apk
APK_OUT := agentvoice-debug.apk
RELEASE_SRC := app/build/outputs/apk/release/app-release.apk
RELEASE_OUT := agentvoice.apk

.PHONY: compile_apk release_apk install test clean

local.properties:
	@if [ -n "$$ANDROID_HOME" ]; then echo "sdk.dir=$$ANDROID_HOME" > $@; \
	elif [ -d "$$HOME/Android/Sdk" ]; then echo "sdk.dir=$$HOME/Android/Sdk" > $@; \
	else echo "Set ANDROID_HOME or create local.properties (see local.properties.example)"; exit 1; fi

compile_apk: local.properties
	./gradlew assembleDebug
	cp -f $(APK_SRC) $(APK_OUT)
	@echo "Built $(CURDIR)/$(APK_OUT)"

release_apk: local.properties
	@test -f keystore.properties || (echo "Create keystore.properties first (see README)"; exit 1)
	./gradlew assembleRelease
	cp -f $(RELEASE_SRC) $(RELEASE_OUT)
	@echo "Built $(CURDIR)/$(RELEASE_OUT)"

install: compile_apk
	adb install -r $(APK_OUT)

test: local.properties
	./gradlew testDebugUnitTest

clean:
	./gradlew clean
	rm -f $(APK_OUT) $(RELEASE_OUT)
