package app

import (
	"context"
	"encoding/base64"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/x/ansi"

	"lanegate/tui/internal/client"
	"lanegate/tui/internal/screens"
	"lanegate/tui/internal/ui"
)

// screenID identifies one of the top-level screens the app can route to.
type screenID int

const (
	screenBoard screenID = iota
	screenTicket
	screenBlocked
	screenDiff
	screenRun
	screenHistory
	screenSettings
)

// screenName returns the human-readable label used by the status bar.
func screenName(s screenID) string {
	switch s {
	case screenBoard:
		return "Board"
	case screenTicket:
		return "Ticket"
	case screenBlocked:
		return "Blocked"
	case screenDiff:
		return "Diff"
	case screenRun:
		return "Run"
	case screenHistory:
		return "Run History"
	case screenSettings:
		return "Settings"
	default:
		return ""
	}
}

// Model is the top-level Bubble Tea model for the LaneGate TUI. It owns screen
// routing, key handling, and kicking off async data loads against the
// injected client.Client; it does not own endpoint schemas or lifecycle
// policy (that stays in internal/client and Python respectively).
type Model struct {
	client client.Client
	width  int
	height int
	ready  bool

	screen           screenID
	previousScreen   screenID
	loading          bool
	helpVisible      bool
	selectedTicketID string
	scrollOffsets    map[screenID]int

	board    *screens.BoardModel
	ticket   *screens.TicketModel
	blocked  *screens.BlockedModel
	diff     *screens.DiffModel
	run      *screens.RunModel
	settings *screens.SettingsModel

	// runStream and runStreamCancel back the Run screen's live raw-log tail.
	// The stream is only ever opened while the Run screen's Raw Audit Log
	// mode is active and tailing the current run (see updateRunStream) —
	// the default structured Activity pane never opens it. runStreamGen is
	// bumped every time a new stream is started so a runLogMsg delivered
	// after the model has moved on (screen switch, mode switch, or a newer
	// stream replacing this one) is recognized as stale and dropped rather
	// than corrupting the now-active screen's state.
	runStream       *client.ReconnectingStream
	runStreamCancel context.CancelFunc
	runStreamGen    int

	// runActivityWant tracks which run's structured events the Activity
	// pane currently wants: "" for the live/current run, or a specific
	// historical run id once a Run History row has been selected. Used to
	// drop a runEventsLoadedMsg that arrives after the selection moved on.
	runActivityWant string

	// runActivityPollGen/polling drive the default structured Activity pane's
	// periodic refresh. Unlike Raw Audit Log, Activity deliberately does not
	// consume the raw log SSE stream, so it needs a small independent poller
	// to remain live while an executor is running.
	runActivityPollGen int
	runActivityPolling bool
	// runSummaryReqGen/AppliedGen order the Live Outcomes table's
	// GET /api/runs/{id}/summary fetches (2s cadence, alongside Activity):
	// each request captures the next sequence number, and a response is only
	// applied if its gen is newer than the last one actually applied — one of
	// these requests can be slow (it enriches every non-success outcome with
	// on-disk reviewer context) and complete after a later tick's response
	// already landed, and without this guard the older reply would overwrite
	// the newer table with stale per-ticket outcomes.
	runSummaryReqGen     int
	runSummaryAppliedGen int
	// runSnapshotReqGen orders GET /api/runs/current responses. The Activity
	// poll can issue another request before a slow earlier one returns; only
	// the newest request is allowed to replace the cached run snapshot.
	runSnapshotReqGen int
	// runCopyStart is the first body line of an in-progress multi-page copy
	// selection, or -1 when no range has been marked.
	runCopyStart int

	// clipboardEscape is emitted once at the beginning of View. OSC 52 lets
	// compatible terminals copy text while Bubble Tea is in the alternate
	// screen, where tea.Printf intentionally does not write output.
	clipboardEscape string

	statusBar *ui.StatusBar
}

// copyRunPaneToClipboard returns an OSC 52 sequence for the whole currently
// loaded pane. OSC 52 is supported by modern terminals (and forwarded by
// many tmux configurations) without relying on an OS-specific helper.
func (m *Model) copyRunPaneToClipboard() {
	content, count := m.run.CopyText()
	m.copyTextToClipboard(content, count, "Copied current Raw Audit Log page to clipboard.", "Copied all Activity events to clipboard.")
}

// markRunCopyStart records the viewport's current top line as the beginning
// of a multi-page copy range.
func (m *Model) markRunCopyStart() {
	vp := m.viewportFor(m.currentBody())
	vp.SetOffset(m.scrollOffsets[m.screen])
	m.runCopyStart = vp.Offset()
	m.statusBar.SetInfo("Copy range starts here — scroll to the end, then press y.")
}

// copyMarkedRunRangeToClipboard copies the full range from the line marked
// with v through the bottom of the current viewport. It supports selections
// spanning any number of pages without requiring terminal mouse selection.
func (m *Model) copyMarkedRunRangeToClipboard() {
	if m.runCopyStart < 0 {
		m.statusBar.SetInfo("Press v at the start of the range, then scroll and press y.")
		return
	}
	body := ansi.Strip(m.currentBody())
	lines := strings.Split(body, "\n")
	vp := m.viewportFor(body)
	vp.SetOffset(m.scrollOffsets[m.screen])
	end := vp.Offset() + m.bodyHeight()
	if end > len(lines) {
		end = len(lines)
	}
	start := m.runCopyStart
	if start < 0 {
		start = 0
	}
	if start >= end {
		m.statusBar.SetInfo("Scroll below the marked start before copying.")
		return
	}
	content := strings.Join(lines[start:end], "\n")
	m.runCopyStart = -1
	m.copyTextToClipboard(content, end-start, "Copied marked Raw Audit Log range to clipboard.", "Copied marked Run range to clipboard.")
}

func (m *Model) copyTextToClipboard(content string, count int, auditMessage, activityMessage string) {
	if count == 0 {
		m.statusBar.SetInfo("Nothing loaded to copy yet.")
		return
	}
	m.clipboardEscape = "\x1b]52;c;" + base64.StdEncoding.EncodeToString([]byte(content)) + "\a"
	if m.run.IsAuditMode() {
		m.statusBar.SetInfo(auditMessage)
	} else {
		m.statusBar.SetInfo(activityMessage)
	}
}

// consumeClipboardEscape returns the queued terminal clipboard sequence once
// so ordinary renders do not repeatedly replace the user's clipboard.
func (m *Model) consumeClipboardEscape() string {
	escape := m.clipboardEscape
	m.clipboardEscape = ""
	return escape
}

// New creates a new top-level app model
func New(c client.Client) *Model {
	return &Model{
		client: c,
		ready:  false,
		screen: screenBoard,
		scrollOffsets: map[screenID]int{
			screenBoard:    0,
			screenTicket:   0,
			screenBlocked:  0,
			screenDiff:     0,
			screenRun:      0,
			screenHistory:  0,
			screenSettings: 0,
		},
		board:        screens.NewBoardModel(),
		ticket:       screens.NewTicketModel(),
		blocked:      screens.NewBlockedModel(),
		diff:         screens.NewDiffModel(),
		run:          screens.NewRunModel(),
		settings:     screens.NewSettingsModel(),
		runCopyStart: -1,
		statusBar: func() *ui.StatusBar {
			sb := ui.NewStatusBar()
			sb.SetScreen(screenName(screenBoard))
			return sb
		}(),
	}
}

// Init initializes the model (Bubble Tea interface). It kicks off the
// initial Board data load so the app is populated as soon as the terminal
// program starts, rather than requiring an explicit key press first.
func (m *Model) Init() tea.Cmd {
	m.loading = true
	return m.loadBoardCmd()
}

// --- Async load messages ---
//
// Each fetch runs as a standard Bubble Tea async command: a closure that
// calls the injected client.Client and returns a typed message, handled in
// Update. Screens never block Update on network/fixture I/O directly.

type boardLoadedMsg struct {
	data *client.BoardPayload
	err  error
}

type ticketLoadedMsg struct {
	data *client.TicketDetail
	err  error
}

type blockedLoadedMsg struct {
	data *client.BlockedPayload
	err  error
}

type diffLoadedMsg struct {
	data *client.DiffPayload
	err  error
}

type runLoadedMsg struct {
	data           *client.RunPayload
	err            error
	autoRefreshing bool
	gen            int
}

// runEventsLoadedMsg carries a GET /api/runs/{id}/events result for the
// Run screen's default Activity pane. runID echoes the request ("" for the
// live/current run) so a response for a selection the user has since moved
// away from can be recognized as stale and dropped.
type runEventsLoadedMsg struct {
	runID string
	data  *client.RunEventsPayload
	err   error
}

// runSummaryLoadedMsg carries a GET /api/runs/{id}/summary result used to
// populate the Run screen's live, incrementally-updated per-ticket
// outcome table while the run is still in progress. gen is the request's
// sequence number (see runSummaryReqGen) so a slower, older request that
// completes after a newer one cannot clobber the table with stale data.
type runSummaryLoadedMsg struct {
	runID string
	gen   int
	data  *client.RunSummaryPayload
	err   error
}

// runActivityPollMsg is emitted by the default Activity pane's periodic
// refresh timer. gen makes a timer scheduled before a screen/mode change
// harmless once it arrives.
type runActivityPollMsg struct {
	gen int
}

type runHistoryLoadedMsg struct {
	data *client.RunHistoryPayload
	err  error
}

// runLogsLoadedMsg carries a GET /api/runs/{id}/logs raw-audit page result.
// Only fetched while the Run screen's explicit Raw Audit Log mode is active.
type runLogsLoadedMsg struct {
	runID string
	data  *client.RunLogsPayload
	err   error
}

// runHistoryPageSize bounds one Activity-history fetch, matching the live
// tail's own cap (screens.maxLogLines is unexported, but the two
// values are intentionally kept equal) so a page fetch is never larger than
// what the Run screen already holds in memory for the live tail.
const runHistoryPageSize = 200

// runLogHistoryMsg carries the result of one GET /api/runs/current/logs
// page fetch, keyed to the run_id it was requested for so a
// response that arrives after the run has changed can be told apart from a
// stale one (see screens.RunModel.SetHistoryPage).
type runLogHistoryMsg struct {
	runID  string
	offset int
	lines  []string
	levels []string
	styles []string
	err    error
}

type settingsLoadedMsg struct {
	data *client.SettingsPayload
	err  error
}

// poolsLoadedMsg carries a GET /api/pools result, loaded alongside settings
// so the Settings screen's pools section populates on the same screen
// switch/refresh as the rest of the screen.
type poolsLoadedMsg struct {
	data *client.PoolsPayload
	err  error
}

// poolSavedMsg carries the result of a PUT /api/pools/{name}/executors
// reorder save, keyed to the pool name it was saved for so a stale response
// (e.g. the user already moved on) can be told apart from the current one.
type poolSavedMsg struct {
	poolName string
	pool     *client.Pool
	err      error
}

// runLogMsg carries one decoded log event (or a read error) from the Run
// screen's live SSE tail. gen pins it to the runStreamGen that was active
// when the read started, so Update can recognize and drop a message from a
// stream the model has since abandoned.
type runLogMsg struct {
	gen int
	ev  client.LogEvent
	err error
}

func (m *Model) loadBoardCmd() tea.Cmd {
	c := m.client
	return func() tea.Msg {
		data, err := c.GetBoard(context.Background())
		return boardLoadedMsg{data: data, err: err}
	}
}

func (m *Model) loadTicketCmd(ticketID string) tea.Cmd {
	c := m.client
	return func() tea.Msg {
		data, err := c.GetTicketDetail(context.Background(), ticketID)
		return ticketLoadedMsg{data: data, err: err}
	}
}

func (m *Model) loadBlockedCmd() tea.Cmd {
	c := m.client
	return func() tea.Msg {
		data, err := c.GetBlocked(context.Background())
		return blockedLoadedMsg{data: data, err: err}
	}
}

func (m *Model) loadDiffCmd(ticketID string) tea.Cmd {
	c := m.client
	return func() tea.Msg {
		data, err := c.GetDiff(context.Background(), ticketID)
		return diffLoadedMsg{data: data, err: err}
	}
}

func (m *Model) loadRunCmd() tea.Cmd {
	return m.loadRunCmdForRefresh(false)
}

// loadRunActivityRefreshCmd marks its response as background work so it can
// preserve the Run viewport without affecting an overlapping foreground load.
func (m *Model) loadRunActivityRefreshCmd() tea.Cmd {
	return m.loadRunCmdForRefresh(true)
}

func (m *Model) loadRunCmdForRefresh(autoRefreshing bool) tea.Cmd {
	c := m.client
	m.runSnapshotReqGen++
	gen := m.runSnapshotReqGen
	return func() tea.Msg {
		data, err := c.GetCurrentRun(context.Background())
		return runLoadedMsg{data: data, err: err, autoRefreshing: autoRefreshing, gen: gen}
	}
}

func (m *Model) loadRunHistoryCmd() tea.Cmd {
	c := m.client
	return func() tea.Msg {
		data, err := c.GetRunHistory(context.Background())
		return runHistoryLoadedMsg{data: data, err: err}
	}
}

// maybeLoadRunHistoryCmd issues one Activity-history page fetch if
// RunModel.HistoryRequest says one is due (not already loading/failed/
// exhausted, and the live tail's boundary is known); otherwise it is a
// no-op, so callers (a "home" press, an explicit "H" press) can call it
// unconditionally without duplicating RunModel's own gating logic.
func (m *Model) maybeLoadRunHistoryCmd() tea.Cmd {
	offset, limit, ok := m.run.HistoryRequest(runHistoryPageSize)
	if !ok {
		return nil
	}
	runID := ""
	if d := m.run.GetData(); d != nil {
		runID = d.RunID
	}
	m.run.SetHistoryLoading(true)
	c := m.client
	return func() tea.Msg {
		page, err := c.GetRunLogPage(context.Background(), offset, limit)
		if err != nil {
			return runLogHistoryMsg{runID: runID, offset: offset, err: err}
		}
		lines := make([]string, len(page.Events))
		levels := make([]string, len(page.Events))
		styles := make([]string, len(page.Events))
		for i, ev := range page.Events {
			lines[i] = ev.Message
			levels[i] = ev.Level
			styles[i] = ev.Style
		}
		return runLogHistoryMsg{runID: runID, offset: offset, lines: lines, levels: levels, styles: styles}
	}
}

func (m *Model) loadRunLogsCmd(runID string, offset, limit int) tea.Cmd {
	c := m.client
	return func() tea.Msg {
		data, err := c.GetRunLogs(context.Background(), runID, offset, limit)
		return runLogsLoadedMsg{runID: runID, data: data, err: err}
	}
}

// loadRunEventsCmd fetches safe structured events for runID ("" for
// the live/current run) for the Run screen's default Activity pane.
func (m *Model) loadRunEventsCmd(runID string) tea.Cmd {
	c := m.client
	return func() tea.Msg {
		data, err := c.GetRunEvents(context.Background(), runID)
		return runEventsLoadedMsg{runID: runID, data: data, err: err}
	}
}

// loadRunSummaryCmd fetches the current run's per-ticket outcome snapshot
// for the Run screen's live Live Outcomes table.
func (m *Model) loadRunSummaryCmd(runID string) tea.Cmd {
	c := m.client
	m.runSummaryReqGen++
	gen := m.runSummaryReqGen
	return func() tea.Msg {
		data, err := c.GetRunSummary(context.Background(), runID)
		return runSummaryLoadedMsg{runID: runID, gen: gen, data: data, err: err}
	}
}

const runActivityPollInterval = 2 * time.Second

// ensureRunActivityPolling schedules Activity refreshes if the default pane
// is visible. It is idempotent so manual refreshes cannot create concurrent
// poll loops.
func (m *Model) ensureRunActivityPolling() tea.Cmd {
	if m.runActivityPolling || m.screen != screenRun || m.run.IsAuditMode() || m.runActivityWant != "" {
		return nil
	}
	m.runActivityPolling = true
	m.runActivityPollGen++
	return m.nextRunActivityPollCmd(m.runActivityPollGen)
}

// scrollToSelectedRunHistory brings the selected Run History table row into
// view. The table's STARTED column no longer echoes the raw run id (it
// shows a formatted local timestamp instead — see run.go), and the "Selected
// Run:" detail line below the table always contains the true run id
// regardless of which row is being scrolled to, so a text search for that
// id would match the wrong line. Use the table's known fixed layout instead.
func (m *Model) scrollToSelectedRunHistory() {
	if line, ok := m.run.SelectedRunRenderedLine(); ok {
		m.scrollToRenderedLine(line)
	}
}

func (m *Model) nextRunActivityPollCmd(gen int) tea.Cmd {
	return tea.Tick(runActivityPollInterval, func(time.Time) tea.Msg {
		return runActivityPollMsg{gen: gen}
	})
}

// stopRunActivityPolling invalidates an already-scheduled timer. Bubble Tea
// timers cannot be canceled directly, but their later message is discarded.
func (m *Model) stopRunActivityPolling() {
	m.runActivityPolling = false
	m.runActivityPollGen++
}

func (m *Model) loadSettingsCmd() tea.Cmd {
	c := m.client
	return func() tea.Msg {
		data, err := c.GetSettings(context.Background())
		return settingsLoadedMsg{data: data, err: err}
	}
}

func (m *Model) loadPoolsCmd() tea.Cmd {
	c := m.client
	return func() tea.Msg {
		data, err := c.GetPools(context.Background())
		return poolsLoadedMsg{data: data, err: err}
	}
}

// savePoolExecutorsCmd persists a reordered executors list for poolName via
// PUT /api/pools/{name}/executors.
func (m *Model) savePoolExecutorsCmd(poolName string, executors []string) tea.Cmd {
	c := m.client
	return func() tea.Msg {
		pool, err := c.UpdatePoolExecutors(context.Background(), poolName, executors)
		return poolSavedMsg{poolName: poolName, pool: pool, err: err}
	}
}

// startRunLogStream (re)opens the Run screen's live log stream, canceling
// any previous one first. It returns the Cmd that reads the stream's first
// event; readRunLogCmd re-issues itself on every subsequent event so the
// stream is consumed for as long as its generation stays current.
func (m *Model) startRunLogStream() tea.Cmd {
	if m.runStreamCancel != nil {
		m.runStreamCancel()
	}
	ctx, cancel := context.WithCancel(context.Background())
	m.runStreamCancel = cancel
	m.runStreamGen++
	m.runStream = client.NewReconnectingStream(ctx, m.client.OpenRunLogStream)
	return m.readRunLogCmd(m.runStreamGen)
}

// stopRunLogStream cancels the active log stream, if any, without starting a
// new one (used when navigating away from the Run screen).
func (m *Model) stopRunLogStream() {
	if m.runStreamCancel != nil {
		m.runStreamCancel()
		m.runStreamCancel = nil
	}
	m.runStream = nil
	m.runStreamGen++
}

// readRunLogCmd reads exactly one event (or error) from the stream captured
// at gen's creation time. client.ReconnectingStream already retries once
// internally on a dropped connection; Update re-issues this Cmd on every
// result (success or error) so a drop that exhausts that internal retry
// still gets picked up and reconnected on the next read.
func (m *Model) readRunLogCmd(gen int) tea.Cmd {
	stream := m.runStream
	return func() tea.Msg {
		ev, err := stream.Next()
		return runLogMsg{gen: gen, ev: ev, err: err}
	}
}
