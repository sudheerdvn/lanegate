package app

// KeyBindings holds all keyboard shortcuts for the application
type KeyBindings struct {
	Global   GlobalKeys
	Board    ScreenKeys
	Ticket   ScreenKeys
	Blocked  ScreenKeys
	Diff     ScreenKeys
	Run      ScreenKeys
	Settings ScreenKeys
}

// GlobalKeys defines application-wide shortcuts
type GlobalKeys struct {
	Quit     string
	Help     string
	Refresh  string
	Tab      string
	ShiftTab string
	Screen1  string
	Screen2  string
	Screen3  string
	Screen4  string
	Screen5  string
	Screen6  string
	Back     string
}

// ScreenKeys defines common screen-level shortcuts
type ScreenKeys struct {
	Up          string
	Down        string
	Left        string
	Right       string
	Enter       string
	PageUp      string
	PageDown    string
	Home        string
	End         string
	Filter      string
	ReorderPool string
	MovePool    string
	// ToggleAudit, NextPage, PrevPage are Run-screen-only (TICK-324): switch
	// between the default structured Activity pane and the explicit,
	// paginated Raw Audit Log, and page through the latter.
	ToggleAudit string
	NextPage    string
	PrevPage    string
	LoadHistory string
}

// DefaultKeyBindings returns the documented key binding set
func DefaultKeyBindings() KeyBindings {
	return KeyBindings{
		Global: GlobalKeys{
			Quit:     "q or ctrl+c",
			Help:     "?",
			Refresh:  "r",
			Tab:      "tab",
			ShiftTab: "shift+tab",
			Screen1:  "1",
			Screen2:  "2",
			Screen3:  "3",
			Screen4:  "4",
			Screen5:  "5",
			Screen6:  "6",
			Back:     "esc",
		},
		Board: ScreenKeys{
			Up:       "up or k",
			Down:     "down or j",
			Left:     "left or h",
			Right:    "right or l",
			Enter:    "enter",
			PageUp:   "pgup or Page Up",
			PageDown: "pgdn or Page Down",
			Home:     "home",
			End:      "end",
			Filter:   "/ (not implemented in MVP)",
		},
		Ticket: ScreenKeys{
			Up:       "up or k",
			Down:     "down or j",
			PageUp:   "pgup or Page Up",
			PageDown: "pgdn or Page Down",
			Home:     "home",
			End:      "end",
		},
		Blocked: ScreenKeys{
			Up:       "up or k",
			Down:     "down or j",
			Enter:    "enter",
			PageUp:   "pgup or Page Up",
			PageDown: "pgdn or Page Down",
			Home:     "home",
			End:      "end",
		},
		Diff: ScreenKeys{
			Up:       "up or k",
			Down:     "down or j",
			PageUp:   "pgup or Page Up",
			PageDown: "pgdn or Page Down",
			Home:     "home",
			End:      "end",
		},
		Run: ScreenKeys{
			Up:          "up or k (move run-history selection)",
			Down:        "down or j (move run-history selection)",
			PageUp:      "pgup or Page Up",
			PageDown:    "pgdn or Page Down",
			Home:        "home (also loads older Activity history, if not already loaded)",
			End:         "end",
			ToggleAudit: "a (toggle Activity / Raw Audit Log)",
			NextPage:    "n (Raw Audit Log: next page)",
			PrevPage:    "N (Raw Audit Log: previous page)",
			LoadHistory: "H (load Activity from before the live tail)",
		},
		Settings: ScreenKeys{
			Up:          "up or k (selects pool, or executor row while reordering)",
			Down:        "down or j (selects pool, or executor row while reordering)",
			Enter:       "enter (save pool reorder)",
			PageUp:      "pgup or Page Up",
			PageDown:    "pgdn or Page Down",
			Home:        "home",
			End:         "end",
			ReorderPool: "p (start reordering the selected pool's executors, esc to cancel)",
			MovePool:    "J/K (move the highlighted executor while reordering)",
		},
	}
}
