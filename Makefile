.PHONY: build-hotkey qa qa-silent qa-real

# Fan rule: the real `swiftc -O` build never runs on this MacBook — it runs
# on `ssh air` via cli/hotkey/build.sh (lane C owns that script). This
# target just calls it by path; it does not build anything itself.
build-hotkey:
	cli/hotkey/build.sh bin/

qa:
	bash qa/run_all.sh

qa-silent:
	PET_TALK_SILENT=1 bash qa/run_all.sh

qa-real:
	PET_TALK_REAL_ENGINE=1 bash qa/run_all.sh
