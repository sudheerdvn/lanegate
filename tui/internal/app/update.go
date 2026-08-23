package app

import (
	tea "github.com/charmbracelet/bubbletea"

	"lanegate/tui/internal/screens"
)

// Update handles messages (Bubble Tea interface)
func (m *Model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		m.ready = true
		return m, nil

	case tea.KeyMsg:
		return m.handleKey(msg)

	case boardLoadedMsg:
		m.board.SetData(msg.data)
		if msg.err == nil {
			if m.selectedTicketID != "" && !m.board.SelectTicket(m.selectedTicketID) {
				m.selectedTicketID = ""
			}
			if m.selectedTicketID == "" {
				m.selectedTicketID = m.board.SelectedTicketID()
			}
		}
		return m, m.finishLoad(screenBoard, msg.err, false)

	case ticketLoadedMsg:
		m.ticket.SetData(msg.data)
		return m, m.finishLoad(screenTicket, msg.err, false)

	case blockedLoadedMsg:
		m.blocked.SetData(msg.data)
		return m, m.finishLoad(screenBlocked, msg.err, false)

	case diffLoadedMsg:
		m.diff.SetData(msg.data)
		return m, m.finishLoad(screenDiff, msg.err, false)

	case runLoadedMsg:
		// A newer GET /api/runs/current request is already in flight, so this
		// response is stale. In particular, do not let its error replace the
		// current screen status or its older snapshot overwrite newer state.
		// Still clear the loading flag: it was set by whichever refresh is
		// tracked by the current gen, and if this stale response turns out
		// to be the last one the model ever sees, skipping this would leave
		// the screen frozen on "Loading Run..." forever.
		if msg.gen != 0 && msg.gen != m.runSnapshotReqGen {
			if m.screen == screenRun {
				m.loading = false
			}
			return m, nil
		}
		// A failed refresh has no payload. Keep the last successful snapshot so
		// the Activity poller and Live Outcomes table have stable state while
		// the status bar reports the transient API error.
		if msg.err == nil && msg.data != nil {
			m.run.SetData(msg.data)
		}
		return m, m.finishLoad(screenRun, msg.err, msg.autoRefreshing)

	case runEventsLoadedMsg:
		if !m.runPaneVisible() || m.run.IsAuditMode() || msg.runID != m.runActivityWant {
			// Stale: the Activity focus has since moved to a different run.
			return m, nil
		}
		if msg.err != nil {
			m.run.SetActivityError(msg.runID, msg.err)
		} else {
			m.run.SetActivityEvents(msg.runID, msg.data)
		}
		return m, m.ensureRunActivityPolling()

	case runSummaryLoadedMsg:
		// Supplementary data for the live Live Outcomes table, not the
		// primary pane — an error here should not surface a blocking error
		// state, just leave the table as it was. Also drop a response older
		// than the last one already applied: these fetches enrich every
		// non-success outcome from disk and can take long enough that an
		// earlier tick's request completes after a later tick's, which would
		// otherwise clobber the table with stale per-ticket outcomes.
		if msg.gen <= m.runSummaryAppliedGen {
			return m, nil
		}
		if msg.err == nil && msg.data != nil {
			m.runSummaryAppliedGen = msg.gen
			m.run.SetLiveBatchTickets(msg.data.BatchTickets)
		}
		return m, nil

	case runActivityPollMsg:
		if msg.gen != m.runActivityPollGen || !m.runActivityPolling || m.screen != screenRun || m.run.IsAuditMode() {
			// This generation's chain is being dropped without rescheduling
			// itself. If runActivityPolling is still marked live for this
			// same generation, clear it here — otherwise it stays stuck
			// true forever and ensureRunActivityPolling's guard permanently
			// refuses to restart polling.
			if msg.gen == m.runActivityPollGen && m.runActivityPolling {
				m.stopRunActivityPolling()
			}
			return m, nil
		}
		// Refresh the complete live Run view alongside structured progress: a
		// worker can finish or a new worker can launch between progress events.
		cmds := []tea.Cmd{
			m.loadRunActivityRefreshCmd(),
			m.loadRunHistoryCmd(),
			m.loadRunEventsCmd(m.runActivityWant),
		}
		if data := m.run.GetData(); data != nil && data.RunID != "" {
			runID := data.RunID
			cmds = append(cmds, m.loadRunSummaryCmd(runID))
		}
		cmds = append(cmds, m.nextRunActivityPollCmd(msg.gen))
		return m, tea.Batch(cmds...)

	case runLogsLoadedMsg:
		if !m.runPaneVisible() || !m.run.IsAuditMode() || msg.runID != m.focusedRunID() {
			return m, nil
		}
		if msg.err != nil {
			m.run.SetAuditError(msg.err)
		} else {
			m.run.SetAuditLogs(msg.data)
		}
		return m, nil

	case runHistoryLoadedMsg:
		if msg.err == nil && msg.data != nil {
			m.run.SetHistory(msg.data)
		}
		return m, m.finishLoad(screenHistory, msg.err, false)

	case settingsLoadedMsg:
		if msg.err != nil {
			m.settings.SetError(msg.err)
		} else {
			m.settings.SetData(msg.data)
		}
		return m, m.finishLoad(screenSettings, msg.err, false)

	case poolsLoadedMsg:
		// Loaded alongside settingsLoadedMsg (see refreshCmd); loading/error
		// status for the screen is driven by settingsLoadedMsg's finishLoad
		// call, not this one, so a slower/failed pools fetch doesn't mask an
		// otherwise-successful settings load.
		if msg.err != nil {
			m.settings.SetPoolsError(msg.err)
		} else {
			m.settings.SetPools(msg.data.Pools)
		}
		return m, nil

	case poolSavedMsg:
		if msg.err != nil {
			if m.screen == screenSettings {
				m.statusBar.SetError(msg.err.Error())
			}
			return m, nil
		}
		if msg.pool != nil {
			m.settings.CommitPoolEdit(msg.pool.Executors)
		}
		if m.screen == screenSettings {
			m.statusBar.SetInfo("Pool order saved.")
		}
		return m, nil

	case runLogMsg:
		if msg.gen != m.runStreamGen || !m.runPaneVisible() || !m.run.IsAuditMode() {
			// Stale: either a newer stream replaced this one, or the Run
			// screen was left and the stream was stopped.
			return m, nil
		}
		if msg.err != nil {
			m.run.SetStreamError(msg.err)
		} else {
			m.run.AppendLogEvent(msg.ev)
		}
		return m, m.readRunLogCmd(msg.gen)

	case runLogHistoryMsg:
		if msg.err != nil {
			m.run.SetHistoryError(msg.err)
		} else {
			m.run.SetHistoryPageWithMetadata(msg.runID, msg.offset, msg.lines, msg.levels, msg.styles)
		}
		return m, nil
	}

	return m, nil
}

// handleKey implements the global key bindings from DefaultKeyBindings plus
// the read-only screen-local selection and scrolling keys used by the MVP.
func (m *Model) handleKey(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch msg.String() {
	case "q", "ctrl+c":
		return m, tea.Quit
	}

	if m.helpVisible {
		switch msg.String() {
		case "?", "esc":
			m.helpVisible = false
			m.statusBar.ClearInfo()
		}
		return m, nil
	}

	m.statusBar.ClearInfo()
	switch msg.String() {
	case "1":
		return m, m.switchScreen(screenBoard)
	case "2":
		return m, m.switchScreen(screenTicket)
	case "3":
		return m, m.switchScreen(screenBlocked)
	case "4":
		return m, m.switchScreen(screenDiff)
	case "5":
		return m, m.switchScreen(screenRun)
	case "6":
		return m, m.switchScreen(screenHistory)
	case "7":
		return m, m.switchScreen(screenSettings)

	case "esc":
		if m.screen == screenSettings && m.settings.IsEditingPool() {
			m.settings.CancelPoolEdit()
			m.statusBar.ClearInfo()
			return m, nil
		}
		if m.screen == screenHistory && m.run.IsHistoryDetail() {
			m.run.CloseHistoryDetail()
			m.runActivityWant = ""
			m.scrollOffsets[screenHistory] = 0
			return m, nil
		}
		if m.previousScreen == m.screen {
			// No navigation history yet; nothing to go back to.
			return m, nil
		}
		return m, m.switchScreen(m.previousScreen)

	case "?":
		m.helpVisible = true
		return m, nil

	case "r":
		return m, m.refreshCmd()
	case "m":
		if m.screen == screenBoard {
			m.board.ToggleGrouping()
			m.selectedTicketID = m.board.SelectedTicketID()
			m.scrollOffsets[screenBoard] = 0
			if line, ok := m.board.SelectedTicketRenderedLine(m.width); ok {
				m.scrollToRenderedLine(line)
			}
			m.statusBar.SetInfo("Board grouped by " + m.board.GroupingLabel() + ".")
		}
		return m, nil

	// p / enter / J / K drive the pools.<name>.executors reorder control
	// on the Settings screen; they are no-ops elsewhere.
	case "p":
		if m.screen == screenSettings && !m.settings.IsEditingPool() {
			if m.settings.BeginPoolEdit() {
				m.statusBar.SetInfo("Reordering pool executors — up/down select, J/K move, enter to save, esc to cancel")
			} else {
				m.statusBar.SetInfo("No pools configured for this project.")
			}
		}
		return m, nil
	case "enter":
		if m.screen == screenSettings && m.settings.IsEditingPool() {
			pool, ok := m.settings.SelectedPool()
			if !ok {
				m.settings.CancelPoolEdit()
				return m, nil
			}
			m.statusBar.SetInfo("Saving pool order...")
			return m, m.savePoolExecutorsCmd(pool.Name, m.settings.PendingOrder())
		}
		switch m.screen {
		case screenBoard, screenBlocked:
			if m.selectedTicketID != "" {
				return m, m.switchScreen(screenTicket)
			}
		case screenHistory:
			return m, m.openSelectedRunCmd()
		}
		return m, nil
	case "J":
		if m.screen == screenSettings && m.settings.IsEditingPool() {
			m.settings.MoveExecutor(1)
		}
		return m, nil
	case "K":
		if m.screen == screenSettings && m.settings.IsEditingPool() {
			m.settings.MoveExecutor(-1)
		}
		return m, nil

	case "up", "k":
		m.moveSelectionOrScroll(-1)
		return m, nil
	case "down", "j":
		m.moveSelectionOrScroll(1)
		return m, nil

	// a / n / N drive the Run screen's Activity <-> Raw Audit Log toggle and
	// the audit log's page navigation; no-ops elsewhere.
	case "a":
		if m.runPaneVisible() {
			return m, m.toggleRunMode()
		}
		return m, nil
	case "c":
		if m.runPaneVisible() {
			m.copyRunPaneToClipboard()
		}
		return m, nil
	case "y":
		if m.runPaneVisible() {
			m.copyMarkedRunRangeToClipboard()
		}
		return m, nil
	case "v":
		if m.runPaneVisible() {
			m.markRunCopyStart()
		}
		return m, nil
	case "n":
		if m.runPaneVisible() && m.run.IsAuditMode() {
			return m, m.pageAuditCmd(1)
		}
		return m, nil
	case "N":
		if m.runPaneVisible() && m.run.IsAuditMode() {
			return m, m.pageAuditCmd(-1)
		}
		return m, nil

	case "pgup", "pageup":
		m.scrollActive(-m.pageScrollAmount())
		return m, nil
	case "pgdown", "pgdn", "pagedown":
		m.scrollActive(m.pageScrollAmount())
		return m, nil
	case "home":
		m.scrollHome()
		if m.runPaneVisible() {
			return m, m.maybeLoadRunHistoryCmd()
		}
		return m, nil
	case "end":
		m.scrollEnd()
		return m, nil
	case "H":
		if m.runPaneVisible() {
			if m.run.HistoryError() != "" {
				m.run.RetryHistory()
			}
			return m, m.maybeLoadRunHistoryCmd()
		}
		return m, nil
	case "tab", "shift+tab":
		m.statusBar.SetInfo("Pane focus is not available on this read-only screen yet.")
		return m, nil
	case "/":
		m.statusBar.SetInfo("Local search and filtering are not available yet.")
		return m, nil
	}

	return m, nil
}

// switchScreen makes target the active screen (recording the prior screen
// for a single-level "esc" back step) and triggers a fresh load for it. The
// Run screen's live raw-log stream is stopped when leaving it, since it is
// the only screen with a standing connection; entering it resets the
// Activity/Raw Audit Log focus back to the default (live run, Activity
// mode) rather than persisting whatever was selected last visit. Leaving
// the History screen for anywhere else similarly closes any open history
// detail — otherwise a later number-key jump back into History (bypassing
// the "esc" handler that normally closes detail) reopens the same stale
// detail view instead of the run list.
func (m *Model) switchScreen(target screenID) tea.Cmd {
	m.helpVisible = false
	if target != m.screen {
		if m.screen == screenRun || m.screen == screenHistory {
			m.stopRunLogStream()
			m.stopRunActivityPolling()
			m.runCopyStart = -1
		}
		if target == screenRun {
			m.run.SetMode(screens.RunModeActivity)
			m.runActivityWant = ""
		}
		if m.screen == screenHistory && m.run.IsHistoryDetail() {
			m.run.CloseHistoryDetail()
			m.runActivityWant = ""
			m.scrollOffsets[screenHistory] = 0
		}
		m.previousScreen = m.screen
		m.screen = target
	}
	m.statusBar.SetScreen(screenName(target))
	m.statusBar.ClearInfo()
	return m.refreshCmd()
}

// refreshCmd (re)loads data for the currently active screen without
// changing screen/history state, matching the documented "r" behavior:
// "Refresh the current screen from the active client."
func (m *Model) refreshCmd() tea.Cmd {
	m.loading = true
	m.statusBar.ClearError()
	m.statusBar.ClearInfo()
	switch m.screen {
	case screenBoard:
		return m.loadBoardCmd()
	case screenTicket:
		return m.loadTicketCmd(m.selectedTicketID)
	case screenBlocked:
		return m.loadBlockedCmd()
	case screenDiff:
		return m.loadDiffCmd(m.selectedTicketID)
	case screenRun:
		cmds := []tea.Cmd{
			m.loadRunCmd(),
			m.loadRunEventsCmd(m.runActivityWant),
		}
		if m.run.IsAuditMode() {
			m.run.SetAuditLoading(true)
			cmds = append(cmds, m.loadRunLogsCmd(m.focusedRunID(), m.run.AuditOffset(), m.run.AuditLimit()))
			if m.runActivityWant == "" {
				cmds = append(cmds, m.startRunLogStream())
			}
		}
		return tea.Batch(cmds...)
	case screenHistory:
		cmds := []tea.Cmd{m.loadRunHistoryCmd()}
		if m.run.IsHistoryDetail() {
			cmds = append(cmds, m.loadRunEventsCmd(m.runActivityWant))
			if m.run.IsAuditMode() {
				m.run.SetAuditLoading(true)
				cmds = append(cmds, m.loadRunLogsCmd(m.focusedRunID(), m.run.AuditOffset(), m.run.AuditLimit()))
			}
		}
		return tea.Batch(cmds...)
	case screenSettings:
		return tea.Batch(m.loadSettingsCmd(), m.loadPoolsCmd())
	}
	return nil
}

// focusedRunID returns the run id the Run screen's Activity/Raw Audit Log
// panes currently want: the selected historical run if one was chosen via
// Run History navigation, else the live/current run's own id (falling back
// to the "current" alias before the first GetCurrentRun response lands).
func (m *Model) focusedRunID() string {
	if m.runActivityWant != "" {
		return m.runActivityWant
	}
	if d := m.run.GetData(); d != nil && d.RunID != "" {
		return d.RunID
	}
	return "current"
}

// runPaneVisible reports whether the active screen currently renders an
// Activity or Raw Audit Log pane. The live Run screen always does; the Run
// History screen does only after a historical row has been opened.
func (m *Model) runPaneVisible() bool {
	return m.screen == screenRun || (m.screen == screenHistory && m.run.IsHistoryDetail())
}

// openSelectedRunCmd loads the currently highlighted historical run into its
// detail screen. History navigation itself remains local and instant; Enter
// is the explicit action that opens Activity or Raw Audit Log data.
func (m *Model) openSelectedRunCmd() tea.Cmd {
	if m.screen != screenHistory || !m.run.OpenSelectedHistory() {
		m.statusBar.SetInfo("No historical run is available to open.")
		return nil
	}
	sel := m.run.SelectedRun()
	m.runActivityWant = sel.RunID
	m.runCopyStart = -1
	m.scrollOffsets[screenHistory] = 0
	if m.run.IsAuditMode() {
		return tea.Batch(m.loadRunEventsCmd(sel.RunID), m.loadAuditPageCmd(0))
	}
	return m.loadRunEventsCmd(sel.RunID)
}

// toggleRunMode switches the Run screen between the default Activity pane
// and the explicit Raw Audit Log pane. Entering audit mode loads its first
// page and, only when focused on the live/current run, opens the raw log
// SSE tail; leaving it stops that tail, since the default Activity pane
// must never hold the raw stream open.
func (m *Model) toggleRunMode() tea.Cmd {
	if m.run.IsAuditMode() {
		m.run.SetMode(screens.RunModeActivity)
		m.stopRunLogStream()
		return m.ensureRunActivityPolling()
	}
	m.run.SetMode(screens.RunModeAudit)
	m.stopRunActivityPolling()
	return m.loadAuditPageCmd(0)
}

// loadAuditPageCmd loads the Raw Audit Log page at offset for the currently
// focused run, and (re)starts the live raw-log tail when that run is the
// live/current one — the tail is never opened for a historical run's audit
// page, since /logs/stream only ever tails the current run.
func (m *Model) loadAuditPageCmd(offset int) tea.Cmd {
	m.run.SetAuditLoading(true)
	cmds := []tea.Cmd{m.loadRunLogsCmd(m.focusedRunID(), offset, m.run.AuditLimit())}
	if m.runActivityWant == "" {
		cmds = append(cmds, m.startRunLogStream())
	} else {
		m.stopRunLogStream()
	}
	return tea.Batch(cmds...)
}

// pageAuditCmd moves the Raw Audit Log page by dir pages (+1/-1), clamped to
// [0, total), and is a no-op past the last page.
func (m *Model) pageAuditCmd(dir int) tea.Cmd {
	limit := m.run.AuditLimit()
	offset := m.run.AuditOffset() + dir*limit
	if offset < 0 {
		offset = 0
	}
	if dir > 0 && offset >= m.run.AuditTotal() {
		return nil
	}
	m.run.SetAuditLoading(true)
	return m.loadRunLogsCmd(m.focusedRunID(), offset, limit)
}

// finishLoad applies the outcome of a completed fetch for the target
// screen. A response for a screen that is no longer active (e.g. the user
// switched away before it returned) still updates cached screen data in
// Update above, but must not stomp on the loading/error state of whatever
// screen is now active.
func (m *Model) finishLoad(target screenID, err error, preserveScroll bool) tea.Cmd {
	if m.screen != target {
		return nil
	}
	m.loading = false
	if err == nil && !(target == screenRun && preserveScroll) {
		m.scrollOffsets[target] = 0
	}
	if err != nil {
		m.statusBar.SetError(err.Error())
	} else {
		m.statusBar.ClearError()
	}
	return nil
}

func (m *Model) moveSelectionOrScroll(delta int) {
	switch m.screen {
	case screenBoard:
		if m.board.MoveSelection(delta) {
			m.selectedTicketID = m.board.SelectedTicketID()
			// A moved selection index does not correspond to one rendered
			// line: status headers, table headers, and blank separators sit
			// between groups, so a selection landing in a later group can
			// require scrolling by many lines for one ticket of movement.
			// Scroll to the selected row's actual rendered position instead
			// of assuming index delta == line delta.
			if line, ok := m.board.SelectedTicketRenderedLine(m.width); ok {
				m.scrollToRenderedLine(line)
			} else {
				m.scrollActive(delta)
			}
		}
	case screenBlocked:
		if m.blocked.MoveSelection(delta) {
			if id := m.blocked.SelectedTicketID(); id != "" {
				m.selectedTicketID = id
			}
			m.scrollActive(delta)
		}
	case screenRun:
		m.scrollActive(delta)
	case screenHistory:
		if m.run.IsHistoryDetail() {
			m.scrollActive(delta)
		} else if m.run.MoveSelection(delta) {
			m.scrollToSelectedRunHistory()
		} else {
			m.scrollActive(delta)
		}
	case screenSettings:
		// While reordering, up/down move the highlighted executor row
		// instead of the focused pool; either way this is
		// row-selection, not free scrolling, matching Board/Blocked.
		if m.settings.IsEditingPool() {
			m.settings.MoveExecutorSelection(delta)
		} else {
			m.settings.MovePoolSelection(delta)
		}
	default:
		m.scrollActive(delta)
	}
}

func (m *Model) scrollActive(delta int) {
	vp := m.viewportFor(m.currentBody())
	vp.SetOffset(m.scrollOffsets[m.screen])
	if delta < 0 {
		vp.ScrollUp(-delta)
	} else {
		vp.ScrollDown(delta)
	}
	m.scrollOffsets[m.screen] = vp.Offset()
}

// scrollToRenderedLine scrolls the active screen's viewport by the minimum
// amount needed to bring the given rendered line fully on-screen, leaving
// the current offset untouched if it's already visible. Used for Board
// selection, where one ticket of movement can span several rendered lines.
func (m *Model) scrollToRenderedLine(line int) {
	height := m.bodyHeight()
	offset := m.scrollOffsets[m.screen]
	if line < offset {
		offset = line
	} else if height > 0 && line > offset+height-1 {
		offset = line - height + 1
	}
	vp := m.viewportFor(m.currentBody())
	vp.SetOffset(offset)
	m.scrollOffsets[m.screen] = vp.Offset()
}

func (m *Model) scrollHome() {
	vp := m.viewportFor(m.currentBody())
	vp.Home()
	m.scrollOffsets[m.screen] = vp.Offset()
}

func (m *Model) scrollEnd() {
	vp := m.viewportFor(m.currentBody())
	vp.End()
	m.scrollOffsets[m.screen] = vp.Offset()
}

func (m *Model) pageScrollAmount() int {
	height := m.bodyHeight()
	if height <= 1 {
		return 1
	}
	return height - 1
}
