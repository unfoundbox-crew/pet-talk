.PHONY: build-cli build-hotkey qa qa-silent qa-real install-agent uninstall-agent agent-status

# bin/pet-talk-cli — the launcher the hotkey daemon spawns on Option+Tab. It
# used to be an untracked binary nothing built, so a fresh clone gave the
# daemon "Target CLI: (unresolved)" and the chord did nothing. Cheap, no
# compiler, runs anywhere.
build-cli:
	cli/build-cli.sh bin/

# The hotkey build is five Swift files and ~8 s on Apple silicon: it runs
# locally at low priority. It is the one exception to the fan rule (Saurabh,
# 2026-09-12). `air` is Intel and would produce an x86_64 binary that cannot
# run on this Mac, so never route it there. It depends on build-cli because a
# daemon without a CLI to spawn is a daemon whose hotkey is a no-op.
build-hotkey: build-cli
	nice -n 19 cli/hotkey/build.sh bin/

qa:
	bash qa/run_all.sh

qa-silent:
	PET_TALK_SILENT=1 bash qa/run_all.sh

qa-real:
	PET_TALK_REAL_ENGINE=1 bash qa/run_all.sh

# Run the pet-talk duplex server as a launchd user agent instead of a
# terminal process. Renders launchd/com.unfoundbox.pet-talk-server.plist.template
# with this checkout's absolute paths and loads it (bin/agent-ctl.sh).
#   make install-agent --dry-run   prints what would happen, touches nothing
#                                   (make's own -n mode: no ~/Library write,
#                                   no launchctl call)
install-agent:
	bash bin/agent-ctl.sh install

uninstall-agent:
	bash bin/agent-ctl.sh uninstall

agent-status:
	bash bin/agent-ctl.sh status
