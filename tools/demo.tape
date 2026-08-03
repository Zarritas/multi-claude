# The README's demo GIF. Recorded with vhs (https://github.com/charmbracelet/vhs):
#
#     vhs tools/demo.tape
#
# Needs vhs, ttyd and ffmpeg on PATH. Drives tools/demo_app.py, which runs the real TUI
# against the synthetic machine from tools/demo_world.py — no real paths or names.
#
# The story, in order: a machine with several projects → the sessions of one, with what is
# running right now → the team's tab → global search finding both at once.

Output docs/img/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1500
# ~20 rows: enough for the tables and the toasts, without half the frame being empty.
Set Height 450
Set Padding 12
Set Framerate 24
Set PlaybackSpeed 1.0
Set TypingSpeed 60ms

# The recording shows `multi-claude`, which is what a user types; the shell function
# behind it points at the demo app so the take is repeatable and uses no real data.
Hide
Type `multi-claude() { .venv/bin/python tools/demo_app.py; }` Enter
Type "clear" Enter
Sleep 500ms
Show

Type "multi-claude"
Sleep 800ms
Enter
Sleep 3s

# Three projects, most recently active first.
Sleep 2s

# Into the busiest one: the Estado column says which session works and which waits.
Enter
Sleep 4s

# The tab of the repository the team shares: sessions published by other people.
# (The preview panel is left out on purpose — its two toasts would stack on top of the
# ones this step raises, and it already has a still in the README.)
Ctrl+Right
Sleep 4s

# Narrow it to one colleague's work.
Type "/"
Sleep 600ms
Type "author:ana"
Sleep 3s
Escape
Sleep 1s

# Back out to the project list. The wait lets the toasts expire before the last shot.
Escape
Sleep 1s
Escape
Sleep 3s

# And search everything by what was said inside: yours and the team's, in one list.
Type "?"
Sleep 1s
Type "nginx"
Sleep 4s

Ctrl+Q
Sleep 1s
