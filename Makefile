.PHONY: build-hotkey qa qa-silent qa-real

# The hotkey build is five Swift files and ~8 s on Apple silicon: it runs
# locally at low priority. It is the one exception to the fan rule (Saurabh,
# 2026-09-12). `air` is Intel and would produce an x86_64 binary that cannot
# run on this Mac, so never route it there.
build-hotkey:
	nice -n 19 cli/hotkey/build.sh bin/

qa:
	bash qa/run_all.sh

qa-silent:
	PET_TALK_SILENT=1 bash qa/run_all.sh

qa-real:
	PET_TALK_REAL_ENGINE=1 bash qa/run_all.sh
