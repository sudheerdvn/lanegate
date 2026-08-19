# LaneGate demo GIF.
#
# Regenerate whenever the CLI changes:
#   pip install -e .        (once, so `lanegate` is on PATH)
#   vhs docs/assets/demo.tape
#
# Walks the real default lifecycle end to end in a disposable scratch repo:
#   init -> create a ticket -> board -> orchestrate --dry-run (no API keys
#   needed) -> start -> (agent work, simulated here by a direct commit) ->
#   complete -> review (independent verdict) -> merge -> proof the ticket
#   file is a normal git-tracked artifact (git log / cat).
#
# This intentionally does NOT use a live coding-agent executor (that would
# require API keys and be non-deterministic); orchestrate --dry-run shows
# the real planned actions, and the worktree commit stands in for whatever
# an agent would have produced.

Output docs/assets/demo.gif

Require lanegate
Require git

Set Shell "bash"
Set FontSize 16
Set Width 1200
Set Height 600
Set Padding 20
Set Theme "Dracula"
Set TypingSpeed 35ms

Hide
Type 'DEMO=$(mktemp -d); cd "$DEMO" && git init -q && git config user.email demo@example.com && git config user.name Demo && printf "print(\"hello widget\")\n" > widget.py && git add widget.py && git commit -qm "init widget" && clear'
Enter
Sleep 500ms
Show

Type "lanegate init --defaults" Enter
Sleep 2s

Type 'lanegate create "Add a --version flag to widget.py" --no-analyze --milestone demo' Enter
Sleep 1500ms

Type "lanegate claim-file widget.py TICK-001" Enter
Sleep 1s

Type "lanegate open TICK-001" Enter
Sleep 1s

Type "lanegate board" Enter
Sleep 2500ms

Type "lanegate run --dry-run --milestone demo --human-review per_ticket" Enter
Sleep 2500ms

Type "lanegate start TICK-001" Enter
Sleep 2s

Hide
Type 'printf "import sys\n\ndef main():\n    if \"--version\" in sys.argv:\n        print(\"widget 1.0.0\")\n        return\n    print(\"hello widget\")\n\nif __name__ == \"__main__\":\n    main()\n" > .lanegate/worktrees/tick-001/widget.py && clear'
Enter
Sleep 300ms
Show

Type 'git -C .lanegate/worktrees/tick-001 commit -am "TICK-001: add --version flag"' Enter
Sleep 1500ms

Type "lanegate complete TICK-001" Enter
Sleep 1500ms

Type 'lanegate review TICK-001 --verdict approved --summary "Independent review: diff scoped to declared touches; --version flag verified"' Enter
Sleep 2500ms

Type "lanegate merge TICK-001" Enter
Sleep 2s

Type "lanegate board --all" Enter
Sleep 2500ms

Type "git --no-pager log --oneline -- .lanegate/tickets/TICK-001.md" Enter
Sleep 2500ms

Type "cat .lanegate/tickets/TICK-001.md" Enter
Sleep 4s
