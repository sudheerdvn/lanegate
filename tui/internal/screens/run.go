package screens

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"

	"lanegate/tui/internal/client"
	"lanegate/tui/internal/ui"
)

// maxLogLines bounds the in-memory streamed raw-log tail so a long-running
// orchestration run doesn't grow the Run screen's memory without limit. The
// tail only accumulates while Raw Audit Log mode is active (see
// RunMode/IsAuditMode) — the default Activity pane never opens the raw
// stream.
const maxLogLines = 200

// defaultAuditPageLimit is the page size used when (re)loading the Raw
// Audit Log's paginated raw events.
const defaultAuditPageLimit = 50

// RunMode selects which of the Run screen's two panes is displayed: the
// default structured Activity feed (safe executor-progress
// events), or the explicit, paginated Raw Audit Log (raw executor
// stdout/protocol lines) reserved for diagnosis.
type RunMode int

const (
	RunModeActivity RunMode = iota
	RunModeAudit
)

// RunModel represents the orchestration-run screen state: the last fetched
// run/worker snapshot (GET /api/runs/current), the default structured
// Activity feed (GET /api/runs/{id}/events), the explicit
// paginated Raw Audit Log (GET /api/runs/{id}/logs, plus a live SSE tail
// while that mode is active), and structured RunSummary history (GET
// /api/runs).
type RunModel struct {
	data          *client.RunPayload
	history       *client.RunHistoryPayload
	selectedIndex int
	selectedRunID string
	historyDetail bool
	mode          RunMode

	// Structured Activity (safe events) — the default pane.
	// activityRunID is "" while showing the live/current run's events, or a
	// specific run id once a historical Run History row has been selected.
	activityRunID  string
	activityEvents []client.ExecutorEvent
	activityErr    string
	activityLoaded bool

	// liveBatchTickets holds the current run's in-progress per-ticket outcome
	// snapshot (GET /api/runs/{id}/summary), refreshed on the same
	// poll cadence as Activity so the Run screen shows dispatched tickets'
	// outcomes as they land instead of only after the whole run terminates.
	liveBatchTickets []client.TicketOutcome

	// Raw Audit Log — explicit RunModeAudit only.
	auditRunID   string
	auditEvents  []client.LogEvent
	auditTotal   int
	auditOffset  int
	auditLimit   int
	auditLoading bool
	auditErr     string
	auditLoaded  bool

	// logLines is the live SSE tail appended while Raw Audit Log mode is
	// tailing the current run.
	logLines      []string
	logLineLevels []string
	logLineStyles []string
	logLineIDs    []int
	streamErr     string

	// Raw Audit Log history (tail pagination)
	historyLines      []string
	historyLineLevels []string
	historyLineStyles []string
	historyRunID      string
	historyCursor     int // 0-indexed file offset history has been loaded back to; -1 = not yet initialized
	historyLoading    bool
	historyErr        string
	historyExhausted  bool
}

// NewRunModel creates a new run model
func NewRunModel() *RunModel {
	return &RunModel{
		data:          &client.RunPayload{},
		mode:          RunModeActivity,
		auditLimit:    defaultAuditPageLimit,
		historyCursor: -1,
	}
}

// SetData updates the run snapshot. A run_id change from the previously seen
// run resets Activity-history state — history fetched for a
// different run is not valid for the current one.
func (rm *RunModel) SetData(data *client.RunPayload) {
	runID := ""
	if data != nil {
		runID = data.RunID
	}
	if runID != "" && runID != rm.historyRunID {
		rm.resetHistory(runID)
	}
	rm.data = data
}

func (rm *RunModel) resetHistory(runID string) {
	rm.historyRunID = runID
	rm.historyLines = nil
	rm.historyLineLevels = nil
	rm.historyLineStyles = nil
	rm.historyCursor = -1
	rm.historyExhausted = false
	rm.historyErr = ""
	rm.historyLoading = false
}

// GetData returns the run snapshot
func (rm *RunModel) GetData() *client.RunPayload {
	return rm.data
}

// Mode returns the active pane (Activity or Raw Audit Log).
func (rm *RunModel) Mode() RunMode {
	return rm.mode
}

// IsAuditMode reports whether the Raw Audit Log pane is active.
func (rm *RunModel) IsAuditMode() bool {
	return rm.mode == RunModeAudit
}

// SetMode switches the active pane.
func (rm *RunModel) SetMode(mode RunMode) {
	rm.mode = mode
}

// SetActivityEvents records the safe structured events for runID ("" for
// the live/current run, otherwise a specific historical run id).
func (rm *RunModel) SetActivityEvents(runID string, payload *client.RunEventsPayload) {
	rm.activityRunID = runID
	rm.activityErr = ""
	rm.activityLoaded = true
	rm.activityEvents = nil
	if payload != nil {
		rm.activityEvents = payload.Events
	}
}

// SetActivityError records a failure to load structured events for runID.
func (rm *RunModel) SetActivityError(runID string, err error) {
	rm.activityRunID = runID
	rm.activityLoaded = true
	rm.activityEvents = nil
	if err == nil {
		rm.activityErr = ""
		return
	}
	rm.activityErr = err.Error()
}

// ActivityEvents returns the currently loaded structured events.
func (rm *RunModel) ActivityEvents() []client.ExecutorEvent {
	return rm.activityEvents
}

// ActivityRunID returns the run id the loaded structured events belong to
// ("" means the live/current run).
func (rm *RunModel) ActivityRunID() string {
	return rm.activityRunID
}

// ActivityError returns the structured-events loading error, if any.
func (rm *RunModel) ActivityError() string {
	return rm.activityErr
}

// SetLiveBatchTickets records the current run's per-ticket outcome snapshot
// refreshed on the Activity poll cadence.
func (rm *RunModel) SetLiveBatchTickets(tickets []client.TicketOutcome) {
	rm.liveBatchTickets = tickets
}

// LiveBatchTickets returns the current run's per-ticket outcome snapshot.
func (rm *RunModel) LiveBatchTickets() []client.TicketOutcome {
	return rm.liveBatchTickets
}

// SetAuditLoading sets whether a Raw Audit Log page fetch is in progress.
func (rm *RunModel) SetAuditLoading(loading bool) {
	rm.auditLoading = loading
}

// SetAuditError records a failure to load a Raw Audit Log page.
func (rm *RunModel) SetAuditError(err error) {
	rm.auditLoading = false
	if err == nil {
		rm.auditErr = ""
		return
	}
	rm.auditErr = err.Error()
}

// SetAuditLogs populates one paginated Raw Audit Log page from GET
// /api/runs/{id}/logs.
func (rm *RunModel) SetAuditLogs(payload *client.RunLogsPayload) {
	rm.auditLoading = false
	rm.auditErr = ""
	rm.auditLoaded = true
	if payload == nil {
		return
	}
	rm.auditRunID = payload.RunID
	rm.auditEvents = payload.Events
	rm.auditTotal = payload.TotalCount
	rm.auditOffset = payload.Offset
	if payload.Limit > 0 {
		rm.auditLimit = payload.Limit
	}
}

// AuditLoadedRunID returns the run id the loaded Raw Audit Log page belongs to.
func (rm *RunModel) AuditLoadedRunID() string {
	return rm.auditRunID
}

// AuditEvents returns the currently loaded Raw Audit Log page.
func (rm *RunModel) AuditEvents() []client.LogEvent {
	return rm.auditEvents
}

// AuditTotal returns the total raw-log event count on the server.
func (rm *RunModel) AuditTotal() int {
	return rm.auditTotal
}

// AuditOffset returns the current Raw Audit Log page offset.
func (rm *RunModel) AuditOffset() int {
	return rm.auditOffset
}

// AuditLimit returns the current Raw Audit Log page size.
func (rm *RunModel) AuditLimit() int {
	if rm.auditLimit <= 0 {
		return defaultAuditPageLimit
	}
	return rm.auditLimit
}

// AuditCanNext reports whether another raw-audit page is available.
func (rm *RunModel) AuditCanNext() bool {
	return rm.auditOffset+len(rm.auditEvents) < rm.auditTotal
}

// AuditCanPrevious reports whether a preceding raw-audit page is available.
func (rm *RunModel) AuditCanPrevious() bool {
	return rm.auditOffset > 0
}

// IsAuditLoading returns whether a Raw Audit Log page is currently loading.
func (rm *RunModel) IsAuditLoading() bool {
	return rm.auditLoading
}

// AuditError returns the Raw Audit Log loading error, if any.
func (rm *RunModel) AuditError() string {
	return rm.auditErr
}

// LogLines returns the currently buffered live raw-log tail.
func (rm *RunModel) LogLines() []string {
	return rm.logLines
}

// SetHistory updates the run history payload
func (rm *RunModel) SetHistory(history *client.RunHistoryPayload) {
	selectedRunID := rm.selectedRunID
	if selected := rm.SelectedRun(); selected != nil {
		selectedRunID = selected.RunID
	}
	rm.history = history
	if selectedRunID != "" && history != nil {
		for i, run := range history.Runs {
			if run.RunID == selectedRunID {
				rm.selectedIndex = i
				rm.selectedRunID = selectedRunID
				return
			}
		}
	}
	rm.selectedRunID = ""
	rm.clampSelection()
}

// GetHistory returns the run history payload
func (rm *RunModel) GetHistory() *client.RunHistoryPayload {
	return rm.history
}

// MoveSelection moves the active run-history row selection by delta.
func (rm *RunModel) MoveSelection(delta int) bool {
	if rm.history == nil || len(rm.history.Runs) == 0 {
		rm.selectedIndex = 0
		return false
	}
	old := rm.selectedIndex
	rm.selectedIndex += delta
	rm.clampSelection()
	rm.selectedRunID = rm.history.Runs[rm.selectedIndex].RunID
	return rm.selectedIndex != old
}

// SelectedIndex returns the active run-history selection index.
func (rm *RunModel) SelectedIndex() int {
	rm.clampSelection()
	return rm.selectedIndex
}

// SelectedRunRenderedLine returns the 0-indexed line within
// RenderHistoryTable's output where the currently selected run's table row
// is drawn. It mirrors that function's fixed structure: a title line, then
// the table's own header row and separator row, before any run rows appear
// — so unlike the Board's grouped table, the offset ahead of row 0 is a
// constant, not something that has to be walked group by group. ok is
// false when there is no history to select from.
func (rm *RunModel) SelectedRunRenderedLine() (int, bool) {
	if rm.history == nil || len(rm.history.Runs) == 0 {
		return 0, false
	}
	rm.clampSelection()
	const titleLine = 1
	const tableHeaderAndSeparator = 2
	return titleLine + tableHeaderAndSeparator + rm.selectedIndex, true
}

// SelectedRun returns the selected RunSummaryPayload, or nil when empty.
func (rm *RunModel) SelectedRun() *client.RunSummaryPayload {
	if rm.history == nil || len(rm.history.Runs) == 0 {
		return nil
	}
	rm.clampSelection()
	return &rm.history.Runs[rm.selectedIndex]
}

// OpenSelectedHistory enters the detail view for the selected historical run.
// It returns false when the history list is empty.
func (rm *RunModel) OpenSelectedHistory() bool {
	selected := rm.SelectedRun()
	if selected == nil {
		return false
	}
	rm.selectedRunID = selected.RunID
	rm.historyDetail = true
	return true
}

// outcomeBreakdown summarizes a run's tickets without collapsing their
// individual outcomes into a misleading run-level verdict.
func outcomeBreakdown(tickets []client.TicketOutcome) string {
	order := []string{"success", "failure", "changes_requested", "skipped", "in_progress", "interrupted"}
	counts := make(map[string]int)
	for _, ticket := range tickets {
		counts[ticket.Outcome]++
	}
	parts := make([]string, 0, len(counts))
	for _, outcome := range order {
		if counts[outcome] > 0 {
			parts = append(parts, fmt.Sprintf("%d %s", counts[outcome], outcome))
			delete(counts, outcome)
		}
	}
	unknown := make([]string, 0, len(counts))
	for outcome := range counts {
		unknown = append(unknown, outcome)
	}
	sort.Strings(unknown)
	for _, outcome := range unknown {
		parts = append(parts, fmt.Sprintf("%d %s", counts[outcome], outcome))
	}
	return strings.Join(parts, " · ")
}

// maxHistoryTicketIDsShown caps how many ticket IDs the Run History list's
// TICKETS column spells out per row. A large batch (a dozen-plus tickets)
// joined unbounded made rows wide enough to push the REASON/OUTCOMES columns
// off the visible terminal width, since Table.Render doesn't wrap or
// truncate to fit — leaving a run's success/failure/stopped breakdown
// effectively invisible.
const maxHistoryTicketIDsShown = 4

// ticketIDsOf joins a run's dispatched ticket IDs so the top-level Run
// History list is scannable without opening each run's detail,
// truncated to maxHistoryTicketIDsShown so REASON/OUTCOMES stay visible.
func ticketIDsOf(tickets []client.TicketOutcome) string {
	ids := make([]string, len(tickets))
	for i, t := range tickets {
		ids[i] = t.TicketID
	}
	if len(ids) > maxHistoryTicketIDsShown {
		return fmt.Sprintf("%s +%d more", strings.Join(ids[:maxHistoryTicketIDsShown], ","), len(ids)-maxHistoryTicketIDsShown)
	}
	return strings.Join(ids, ",")
}

// historyRunType distinguishes an orchestrated lane run, a daemon-triggered auto
// run, and a direct action in the compact history table. Both records
// intentionally share the same RunSummary transport, but a table that only
// shows ticket IDs makes a lane run containing (say) TICK-100 look like a
// ticket-level action. Direct actions have the durable action- prefix by contract;
// resume-watch triggered runs report AUTO; orchestrated sessions default to LANE.
func historyRunType(runID, triggeredBy string) string {
	if strings.HasPrefix(runID, "action-") {
		return "MANUAL"
	}
	if triggeredBy == "resume-watch" {
		return "AUTO"
	}
	return "LANE"
}

// CloseHistoryDetail returns from a historical run's detail view to the list.
func (rm *RunModel) CloseHistoryDetail() {
	rm.historyDetail = false
}

// IsHistoryDetail reports whether a selected historical run is open.
func (rm *RunModel) IsHistoryDetail() bool {
	return rm.historyDetail
}

func (rm *RunModel) clampSelection() {
	if rm.history == nil || len(rm.history.Runs) == 0 {
		rm.selectedIndex = 0
		return
	}
	if rm.selectedIndex < 0 {
		rm.selectedIndex = 0
	}
	if rm.selectedIndex >= len(rm.history.Runs) {
		rm.selectedIndex = len(rm.history.Runs) - 1
	}
}

// AppendLogEvent appends one streamed raw-log line, bounding the buffer to
// the most recent maxLogLines entries. A successful event also clears any
// prior stream error, since it proves the stream is healthy again.
func (rm *RunModel) AppendLogEvent(ev client.LogEvent) {
	rm.streamErr = ""
	if ev.Message == "" {
		return
	}
	id, _ := strconv.Atoi(ev.ID)
	rm.logLines = append(rm.logLines, ev.Message)
	rm.logLineLevels = append(rm.logLineLevels, ev.Level)
	rm.logLineStyles = append(rm.logLineStyles, ev.Style)
	rm.logLineIDs = append(rm.logLineIDs, id)
	if len(rm.logLines) > maxLogLines {
		rm.logLines = rm.logLines[len(rm.logLines)-maxLogLines:]
		rm.logLineLevels = rm.logLineLevels[len(rm.logLineLevels)-maxLogLines:]
		rm.logLineStyles = rm.logLineStyles[len(rm.logLineStyles)-maxLogLines:]
		rm.logLineIDs = rm.logLineIDs[len(rm.logLineIDs)-maxLogLines:]
	}
}

// HistoryRequest returns the (offset, limit) to fetch for the next
// older-than-tail Activity page, and false when a fetch should not be issued
// right now — already in flight, already failed (call RetryHistory first),
// already exhausted (reached the start of the log), or the live tail's
// oldest line id isn't known yet (nothing streamed in, so there is no
// boundary to page backward from).
func (rm *RunModel) HistoryRequest(pageSize int) (offset, limit int, ok bool) {
	if rm.historyLoading || rm.historyErr != "" || rm.historyExhausted {
		return 0, 0, false
	}
	boundary := rm.historyCursor
	if boundary < 0 {
		if len(rm.logLineIDs) == 0 {
			return 0, 0, false
		}
		// logLineIDs[0] is 1-indexed; the count of lines strictly before it
		// (0-indexed) is logLineIDs[0]-1.
		boundary = rm.logLineIDs[0] - 1
	}
	if boundary <= 0 {
		return 0, 0, false
	}
	offset = boundary - pageSize
	if offset < 0 {
		offset = 0
	}
	return offset, boundary - offset, true
}

// SetHistoryLoading records whether an Activity-history fetch is in flight,
// so Render can show a loading indicator instead of leaving the boundary
// ambiguous.
func (rm *RunModel) SetHistoryLoading(loading bool) {
	rm.historyLoading = loading
}

// SetHistoryError records an Activity-history fetch failure. Render surfaces
// it explicitly rather than letting an error masquerade as "start of run".
func (rm *RunModel) SetHistoryError(err error) {
	rm.historyLoading = false
	if err == nil {
		rm.historyErr = ""
		return
	}
	rm.historyErr = err.Error()
}

// RetryHistory clears a prior history-fetch error so HistoryRequest will
// issue a fresh fetch for the same still-outstanding page.
func (rm *RunModel) RetryHistory() {
	rm.historyErr = ""
}

// SetHistoryPage is a backward-compatibility wrapper around
// SetHistoryPageWithLevels for tests that don't need per-line level
// metadata; production code calls SetHistoryPageWithLevels directly.
func (rm *RunModel) SetHistoryPage(runID string, offset int, lines []string) {
	rm.SetHistoryPageWithLevels(runID, offset, lines, nil)
}

// SetHistoryPageWithLevels records a fetched older-Activity page and its
// level metadata. It remains for callers that do not have style tokens.
func (rm *RunModel) SetHistoryPageWithLevels(runID string, offset int, lines, levels []string) {
	rm.SetHistoryPageWithMetadata(runID, offset, lines, levels, nil)
}

// SetHistoryPageWithMetadata records a fetched older-Activity page and its
// display metadata. The separate slices preserve the text-only history API
// while retaining colour for paginated Raw Audit Log entries.
func (rm *RunModel) SetHistoryPageWithMetadata(runID string, offset int, lines, levels, styles []string) {
	rm.historyLoading = false
	if rm.data == nil || rm.data.RunID == "" || runID != rm.data.RunID {
		rm.historyErr = "history unavailable: run changed"
		return
	}
	rm.historyErr = ""
	merged := make([]string, 0, len(lines)+len(rm.historyLines))
	merged = append(merged, lines...)
	merged = append(merged, rm.historyLines...)
	rm.historyLines = merged
	pageLevels := make([]string, len(lines))
	copy(pageLevels, levels)
	mergedLevels := make([]string, 0, len(pageLevels)+len(rm.historyLineLevels))
	mergedLevels = append(mergedLevels, pageLevels...)
	mergedLevels = append(mergedLevels, rm.historyLineLevels...)
	rm.historyLineLevels = mergedLevels
	pageStyles := make([]string, len(lines))
	copy(pageStyles, styles)
	mergedStyles := make([]string, 0, len(pageStyles)+len(rm.historyLineStyles))
	mergedStyles = append(mergedStyles, pageStyles...)
	mergedStyles = append(mergedStyles, rm.historyLineStyles...)
	rm.historyLineStyles = mergedStyles
	rm.historyCursor = offset
	if offset <= 0 {
		rm.historyExhausted = true
	}
}

// HistoryLoading reports whether an Activity-history fetch is in flight.
func (rm *RunModel) HistoryLoading() bool {
	return rm.historyLoading
}

// HistoryError returns the last Activity-history fetch error, or "".
func (rm *RunModel) HistoryError() string {
	return rm.historyErr
}

// HistoryExhausted reports whether Activity history has been loaded back to
// the start of the current run's log.
func (rm *RunModel) HistoryExhausted() bool {
	return rm.historyExhausted
}

// HistoryLines returns the currently loaded older-than-tail Activity, oldest
// first.
func (rm *RunModel) HistoryLines() []string {
	return rm.historyLines
}

// SetStreamError records a run-log stream failure so Render can surface it.
// Passing nil clears it.
func (rm *RunModel) SetStreamError(err error) {
	if err == nil {
		rm.streamErr = ""
		return
	}
	rm.streamErr = err.Error()
}

// lastCooldownOf extracts the most recent executor cooldown from d's nested
// Orchestration field, or nil when either is absent.
func lastCooldownOf(d *client.RunPayload) *client.CooldownEvent {
	if d == nil || d.Orchestration == nil {
		return nil
	}
	return d.Orchestration.LastCooldown
}

// renderResumeWatchSection renders a compact resume-watch daemon status line.
// Returns an empty string when rws is nil (daemon absent). lastCooldown
// names *which* pool executor instance most recently hit a rate-limit
// cooldown — resume-watch's own phase/elapsed_time is instance-agnostic, so
// without it an operator can't tell claude-a from codex from a stalled run.
func renderResumeWatchSection(rws *client.ResumeWatchStatus, lastCooldown *client.CooldownEvent) string {
	if rws == nil {
		return ""
	}
	var b strings.Builder
	b.WriteString(ui.LabelStyle.Render("Resume Watch"))
	b.WriteString("\n")
	switch rws.Phase {
	case "waiting":
		fmt.Fprintf(&b, "  waiting — %.0fs elapsed", rws.ElapsedTime)
	case "retrying":
		fmt.Fprintf(&b, "  retrying — %.0fs elapsed", rws.ElapsedTime)
	case "gave_up":
		fmt.Fprintf(&b, "  gave up after %.0fs — manual resume needed", rws.ElapsedTime)
	default:
		fmt.Fprintf(&b, "  %s", rws.Phase)
	}
	if rws.NextRetryETA != nil {
		fmt.Fprintf(&b, " — next retry at %s", *rws.NextRetryETA)
	}
	b.WriteString("\n")
	if lastCooldown != nil && lastCooldown.Instance != "" {
		fmt.Fprintf(&b, "  rate-limited instance: %s", lastCooldown.Instance)
		if lastCooldown.Reason != "" {
			fmt.Fprintf(&b, " — %s", lastCooldown.Reason)
		}
		b.WriteString("\n")
	}
	return b.String()
}

// renderHistoryStatusLine surfaces the Activity-history boundary/loading/
// error state so loaded history, an in-flight fetch, and a fetch
// failure are visually distinct from each other and from the live tail —
// none of them should read as if they were simply the start of the run.
// Returns "" when there is nothing to say yet (no history requested).
func (rm *RunModel) renderHistoryStatusLine() string {
	switch {
	case rm.historyErr != "":
		return ui.LabelStyle.Render("History:") + " error loading older activity — " + rm.historyErr + " (press H to retry)\n"
	case rm.historyLoading:
		return ui.LabelStyle.Render("History:") + " loading older activity...\n"
	case rm.historyExhausted && len(rm.historyLines) > 0:
		return ui.LabelStyle.Render("History:") + " start of run reached\n"
	case len(rm.historyLines) > 0:
		return ui.LabelStyle.Render("History:") + " loaded from server (press H for more)\n"
	default:
		return ""
	}
}

// renderLiveOutcomesSection renders the incrementally-populated per-ticket
// outcome table for the current run's dispatched batch: each
// ticket fills in here as soon as it reaches a terminal outcome, without
// waiting for the whole run to finish. Tickets still in progress are
// omitted rather than shown with placeholder outcome/duration. Returns ""
// when no dispatched ticket has reached an outcome yet.
func (rm *RunModel) renderLiveOutcomesSection(width int) string {
	terminal := make([]client.TicketOutcome, 0, len(rm.liveBatchTickets))
	for _, t := range rm.liveBatchTickets {
		if t.Outcome != "in_progress" {
			terminal = append(terminal, t)
		}
	}
	if len(terminal) == 0 {
		return ""
	}

	var b strings.Builder
	b.WriteString(ui.LabelStyle.Render("Live Outcomes"))
	b.WriteString("\n")
	table := ui.NewTable([]string{"TICKET", "EXECUTOR", "OUTCOME", "DURATION"}, width)
	for _, t := range terminal {
		table.AddRow([]string{t.TicketID, t.Executor, t.Outcome, fmt.Sprintf("%.1fs", t.DurationSeconds)}, false)
	}
	b.WriteString(table.Render())

	for _, t := range terminal {
		if t.FailureReason != nil && *t.FailureReason != "" {
			fmt.Fprintf(&b, "\n  %s failure reason: %s", t.TicketID, *t.FailureReason)
		}
		if t.ReviewReason != nil && *t.ReviewReason != "" {
			fmt.Fprintf(&b, "\n  %s review reason: %s", t.TicketID, *t.ReviewReason)
		}
	}

	return b.String()
}

// RenderHistoryTable renders just the scrollable part of the Run History
// screen: the title and the table of runs. The selected run's detail is
// rendered separately by RenderHistorySelectedDetail so the app layer can
// pin it below the scrollable table instead of letting it scroll along with
// the rows — with a long history, scrolling far enough to reveal it used to
// push the table's own header (and the row you were looking for) off the
// top instead.
func (rm *RunModel) RenderHistoryTable(width int) string {
	if rm.history == nil || len(rm.history.Runs) == 0 {
		return ""
	}

	var b strings.Builder
	b.WriteString(ui.LabelStyle.Render("Run History"))
	b.WriteString("\n")

	table := ui.NewTable([]string{"STARTED", "TYPE", "REASON", "OUTCOMES", "TICKETS"}, width)
	for i, r := range rm.history.Runs {
		reason := strings.ToUpper(r.Reason)
		// Started shows FormatLocalTS(r.Timestamp) rather than the raw run
		// id: run ids come in two inconsistent shapes (bare session_ts for
		// orchestrate runs vs "action-<ts-with-microseconds>Z" for direct
		// actions), so formatting the shared Timestamp field instead gives
		// every row the same compact, zone-labeled rendering. The exact run
		// id remains available below in "Selected Run:" for correlating with
		// `lanegate ps` or a log filename.
		table.AddRow([]string{ui.FormatLocalTS(r.Timestamp), historyRunType(r.RunID, r.TriggeredBy), reason, outcomeBreakdown(r.BatchTickets), ticketIDsOf(r.BatchTickets)}, i == rm.selectedIndex)
	}
	b.WriteString(table.Render())

	return b.String()
}

// RenderHistorySelectedDetail renders the selected run's reason/timestamp
// and its per-ticket outcome table. See RenderHistoryTable for why this is
// kept separate. Returns "" when there is no history to select from.
func (rm *RunModel) RenderHistorySelectedDetail(width int) string {
	selected := rm.SelectedRun()
	if selected == nil {
		return ""
	}

	var b strings.Builder
	fmt.Fprintf(&b, "%s %s\n", ui.LabelStyle.Render("Selected Run:"), selected.RunID)
	fmt.Fprintf(&b, "%s %s\n", ui.LabelStyle.Render("Terminal Reason:"), selected.Reason)
	if selected.Timestamp != "" {
		fmt.Fprintf(&b, "%s %s\n", ui.LabelStyle.Render("Timestamp:"), ui.FormatLocalTS(selected.Timestamp))
	}

	b.WriteString("\n")
	b.WriteString(ui.LabelStyle.Render("Tickets"))
	b.WriteString("\n")
	if len(selected.BatchTickets) == 0 {
		b.WriteString("(no dispatched tickets)\n")
	} else {
		ticketTable := ui.NewTable([]string{"TICKET", "EXECUTOR", "OUTCOME", "DURATION"}, width)
		for _, t := range selected.BatchTickets {
			dur := fmt.Sprintf("%.1fs", t.DurationSeconds)
			ticketTable.AddRow([]string{t.TicketID, t.Executor, t.Outcome, dur}, false)
		}
		b.WriteString(ticketTable.Render())
		b.WriteString("\n")

		for _, t := range selected.BatchTickets {
			if t.FailureReason != nil && *t.FailureReason != "" {
				fmt.Fprintf(&b, "  %s failure reason: %s\n", t.TicketID, *t.FailureReason)
			}
			if t.ReviewReason != nil && *t.ReviewReason != "" {
				fmt.Fprintf(&b, "  %s review reason: %s\n", t.TicketID, *t.ReviewReason)
			}
		}
	}

	return strings.TrimRight(b.String(), "\n")
}

// renderHistorySection is RenderHistoryTable and RenderHistorySelectedDetail
// combined into one string, for callers that want the whole Run History
// list screen as a single block rather than the app layer's pinned-detail
// layout — used by RenderHistory below and by tests.
func (rm *RunModel) renderHistorySection(width int) string {
	table := rm.RenderHistoryTable(width)
	if table == "" {
		return ""
	}
	detail := rm.RenderHistorySelectedDetail(width)
	if detail == "" {
		return table
	}
	return table + "\n\n" + detail
}

// RenderHistory renders the Run History table, or the selected historical
// run's Activity/Raw Audit Log detail after Enter opens it.
func (rm *RunModel) RenderHistory(width int) string {
	if rm.historyDetail {
		return rm.renderHistoricalRunDetail(width)
	}
	if rm.history == nil || len(rm.history.Runs) == 0 {
		return "(no run history yet)"
	}
	return rm.renderHistorySection(width)
}

// renderHistoricalRunDetail presents a selected completed run without the
// full history table above it, leaving room for its Activity or Raw Audit Log.
func (rm *RunModel) renderHistoricalRunDetail(width int) string {
	selected := rm.SelectedRun()
	if selected == nil {
		return "(historical run is no longer available)"
	}

	var b strings.Builder
	b.WriteString(ui.LabelStyle.Render("Historical Run"))
	b.WriteString("\n")
	fmt.Fprintf(&b, "%s %s\n", ui.LabelStyle.Render("Run ID:"), selected.RunID)
	fmt.Fprintf(&b, "%s %s\n", ui.LabelStyle.Render("Terminal Reason:"), selected.Reason)
	if selected.Timestamp != "" {
		fmt.Fprintf(&b, "%s %s\n", ui.LabelStyle.Render("Timestamp:"), ui.FormatLocalTS(selected.Timestamp))
	}

	b.WriteString("\n")
	b.WriteString(ui.LabelStyle.Render("Tickets"))
	b.WriteString("\n")
	if len(selected.BatchTickets) == 0 {
		b.WriteString("(no dispatched tickets)\n")
	} else {
		table := ui.NewTable([]string{"TICKET", "EXECUTOR", "OUTCOME", "DURATION"}, width)
		for _, t := range selected.BatchTickets {
			table.AddRow([]string{t.TicketID, t.Executor, t.Outcome, fmt.Sprintf("%.1fs", t.DurationSeconds)}, false)
		}
		b.WriteString(table.Render())
		b.WriteString("\n")
		for _, t := range selected.BatchTickets {
			if t.FailureReason != nil && *t.FailureReason != "" {
				fmt.Fprintf(&b, "  %s failure reason: %s\n", t.TicketID, *t.FailureReason)
			}
			if t.ReviewReason != nil && *t.ReviewReason != "" {
				fmt.Fprintf(&b, "  %s review reason: %s\n", t.TicketID, *t.ReviewReason)
			}
		}
	}

	// The selected history row can be the run that is still in progress
	// (e.g. Terminal Reason "running" or "between-dispatches") rather than a
	// completed one; in that case rm.data still holds its live worker state,
	// so show the same Workers/Resolved Dispatch/Batch info the live Render()
	// view would.
	if rm.data != nil && rm.data.RunID != "" && rm.data.RunID == selected.RunID {
		b.WriteString("\n")
		b.WriteString(renderWorkersSection(rm.data, width))
	}

	b.WriteString("\n")
	if rm.mode == RunModeAudit {
		b.WriteString(rm.renderAuditSection(width))
	} else {
		b.WriteString(rm.renderActivitySection(width))
	}
	return strings.TrimRight(b.String(), "\n")
}

// renderWorkersSection renders the live Workers table, Resolved Dispatch,
// and Batch/Under-filled diagnostics for a run snapshot. Shared by the live
// Render() and, when the selected historical run is still the active run,
// renderHistoricalRunDetail() — a completed run has no live worker state.
func renderWorkersSection(d *client.RunPayload, width int) string {
	var b strings.Builder
	b.WriteString(ui.LabelStyle.Render("Workers"))
	b.WriteString("\n")
	if len(d.Workers) == 0 {
		b.WriteString("(no active workers)\n")
	} else {
		table := ui.NewTable([]string{"TICKET", "PID", "STATE", "RECONCILIATION"}, width)
		for _, w := range d.Workers {
			pid := "-"
			if w.ExecutorPID != 0 {
				pid = fmt.Sprintf("%d", w.ExecutorPID)
			}
			table.AddRow([]string{w.TicketID, pid, w.State, w.ReconciliationState}, false)
		}
		b.WriteString(table.Render())
		b.WriteString("\n")

		b.WriteString("\n")
		b.WriteString(ui.LabelStyle.Render("Resolved Dispatch"))
		b.WriteString("\n")
		for _, w := range d.Workers {
			driver := w.ResolvedDriver
			if driver == "" {
				driver = "-"
			}
			executor := w.ResolvedExecutor
			if executor == "" {
				executor = "-"
			}
			model := w.ResolvedModel
			if model == "" {
				model = "-"
			}
			fmt.Fprintf(&b, "%s  route=%s executor=%s model=%s\n", w.TicketID, driver, executor, model)
		}
	}
	if d.BatchLine != "" {
		b.WriteString("\n")
		b.WriteString(ui.LabelStyle.Render("Batch:") + " " + strings.TrimSpace(d.BatchLine))
		b.WriteString("\n")
		if d.UnderfilledReason != nil && *d.UnderfilledReason != "" {
			b.WriteString(ui.LabelStyle.Render("Under-filled:") + " " + ui.WrapText(*d.UnderfilledReason, width))
			b.WriteString("\n")
		}
	}
	return b.String()
}

// progressCategory buckets a safe executor-progress record into one of the
// four semantic Activity categories, so a failed test run, a stalled
// executor, or a waiting/reviewing phase is visually distinguishable from
// ordinary in-progress work.
func progressCategory(p client.ExecutorProgress) ui.ActivityCategory {
	if p.TestSummary != nil && p.TestSummary.Status == "fail" {
		return ui.ActivityCategoryDanger
	}
	switch p.Activity {
	case "stall":
		return ui.ActivityCategoryDanger
	case "provider_wait", "heartbeat":
		return ui.ActivityCategoryWaiting
	case "completed":
		if p.Phase == "reviewing" {
			return ui.ActivityCategoryWaiting
		}
		return ui.ActivityCategorySuccess
	}
	if p.Phase == "reviewing" || p.Phase == "waiting" {
		return ui.ActivityCategoryWaiting
	}
	if p.Activity == "testing" && p.TestSummary != nil && p.TestSummary.Status == "pass" {
		return ui.ActivityCategorySuccess
	}
	return ui.ActivityCategoryActive
}

// activityLabel returns a short, human-readable label for a progress
// record's activity, independent of the color used to render it.
func activityLabel(p client.ExecutorProgress) string {
	switch p.Activity {
	case "planning":
		return "planning"
	case "tool_use":
		return "using tool"
	case "reading_file":
		return "reading file"
	case "writing_file":
		return "writing file"
	case "running_command":
		return "running command"
	case "testing":
		if p.TestSummary != nil {
			switch p.TestSummary.Status {
			case "pass":
				return "tests passed"
			case "fail":
				return "tests failed"
			case "running":
				return "running tests"
			}
		}
		return "running tests"
	case "thinking":
		return "thinking"
	case "searching":
		return "searching"
	case "provider_wait":
		return "waiting on provider"
	case "stall":
		return "stalled"
	case "heartbeat":
		return "heartbeat"
	case "completed":
		return "completed"
	default:
		if p.Activity == "" {
			return "unknown"
		}
		return p.Activity
	}
}

// testSummaryText renders a concise "N passed"/"N failed" test result, or
// the bare status when no counts are present.
func testSummaryText(ts *client.TestSummary) string {
	if ts == nil {
		return ""
	}
	switch ts.Status {
	case "pass":
		if ts.Passed > 0 || ts.Failed > 0 {
			return fmt.Sprintf("%d passed", ts.Passed)
		}
		return "pass"
	case "fail":
		if ts.Failed > 0 {
			return fmt.Sprintf("%d failed", ts.Failed)
		}
		return "fail"
	case "running":
		return "running"
	default:
		return ""
	}
}

// formatEventTime renders ev's timestamp as a compact local HH:MM:SS,
// falling back to the raw string when it cannot be parsed.
func formatEventTime(iso string) string {
	if iso == "" {
		return "--:--:--"
	}
	t, err := time.Parse(time.RFC3339, iso)
	if err != nil {
		return iso
	}
	return t.Local().Format("15:04:05")
}

// formatActivityEvent renders one safe executor-progress record as a single
// compact, bounded line: time, ticket, executor/model, phase, a
// color-coded-but-labeled activity, the bounded affected path when present,
// and a concise test result when present. It never renders raw executor
// stdout, protocol lines, prompts, commands, or reasoning — those fields do
// not exist on client.ExecutorProgress.
func formatActivityEvent(ev client.ExecutorEvent) string {
	p := ev.Progress
	cat := progressCategory(p)
	symbol := ui.ActivityStyle(cat).Render(ui.ActivitySymbol(cat))

	execModel := p.Executor
	if execModel == "" {
		execModel = "unknown"
	}
	if p.Model != "" {
		execModel = execModel + "/" + p.Model
	}

	var b strings.Builder
	fmt.Fprintf(&b, "%s  %s  %s  %s  %s %s", formatEventTime(ev.Ts), ev.TicketID, execModel, p.Phase, symbol, ui.ActivityStyle(cat).Render(activityLabel(p)))
	if p.Path != "" {
		fmt.Fprintf(&b, "  %s", p.Path)
	}
	if summary := testSummaryText(p.TestSummary); summary != "" {
		fmt.Fprintf(&b, " (%s)", summary)
	}
	return b.String()
}

// formatActivityEventPlain is the clipboard/export equivalent of
// formatActivityEvent. It deliberately omits terminal color escapes so the
// copied text remains useful in an issue, chat, or plain-text file.
func formatActivityEventPlain(ev client.ExecutorEvent) string {
	p := ev.Progress
	execModel := p.Executor
	if execModel == "" {
		execModel = "unknown"
	}
	if p.Model != "" {
		execModel += "/" + p.Model
	}

	var b strings.Builder
	fmt.Fprintf(&b, "%s  %s  %s  %s  %s %s", formatEventTime(ev.Ts), ev.TicketID, execModel, p.Phase, ui.ActivitySymbol(progressCategory(p)), activityLabel(p))
	if p.Path != "" {
		fmt.Fprintf(&b, "  %s", p.Path)
	}
	if summary := testSummaryText(p.TestSummary); summary != "" {
		fmt.Fprintf(&b, " (%s)", summary)
	}
	return b.String()
}

// CopyText returns all currently loaded text for the active Run pane without
// viewport truncation or terminal styling. Activity contains the complete
// structured event response; Raw Audit Log is intentionally limited to its
// loaded page plus the bounded live tail.
func (rm *RunModel) CopyText() (text string, itemCount int) {
	if rm.IsAuditMode() {
		lines := make([]string, 0, len(rm.auditEvents)+len(rm.logLines))
		for _, ev := range rm.auditEvents {
			if ev.Message != "" {
				lines = append(lines, ev.Message)
			}
		}
		lines = append(lines, rm.logLines...)
		return strings.Join(lines, "\n"), len(lines)
	}

	lines := make([]string, 0, len(rm.activityEvents))
	for _, ev := range rm.activityEvents {
		lines = append(lines, formatActivityEventPlain(ev))
	}
	return strings.Join(lines, "\n"), len(lines)
}

// activityCategoryForState buckets a worker/reconciliation state string
// (see lanegate/orchestrate/status.py and pool.py) into the same four semantic
// categories used for structured progress events, so the lifecycle fallback
// stays visually consistent with the Activity feed.
func activityCategoryForState(state string) ui.ActivityCategory {
	s := strings.ToLower(state)
	switch {
	case strings.Contains(s, "fail"), strings.Contains(s, "block"), strings.Contains(s, "hibernat"),
		strings.Contains(s, "error"), strings.Contains(s, "stale"), strings.Contains(s, "unreadable"):
		return ui.ActivityCategoryDanger
	case strings.Contains(s, "wait"), strings.Contains(s, "retr"), strings.Contains(s, "review"),
		strings.Contains(s, "pending"), strings.Contains(s, "stall"):
		return ui.ActivityCategoryWaiting
	case strings.Contains(s, "success"), strings.Contains(s, "finish"), strings.Contains(s, "complet"),
		strings.Contains(s, "merged"), strings.Contains(s, "pass"):
		return ui.ActivityCategorySuccess
	default:
		return ui.ActivityCategoryActive
	}
}

// lifecycleFallback renders a concise, bounded lifecycle/heartbeat summary
// for when a run has no structured events recorded yet (older runs, an
// unsupported executor, or a run that just started). It never falls back to
// unbounded raw output.
func (rm *RunModel) lifecycleFallback() string {
	if rm.activityRunID != "" {
		// A historical run is focused; its outcome/reason already appears in
		// the Run History section below.
		return "(no structured activity recorded for this run)"
	}
	d := rm.data
	if d == nil || d.RunID == "" {
		return "(no activity yet)"
	}
	if len(d.Workers) == 0 {
		return "(no active workers)"
	}
	var lines []string
	for _, w := range d.Workers {
		state := w.State
		if state == "" {
			state = "unknown"
		}
		cat := activityCategoryForState(state)
		symbol := ui.ActivityStyle(cat).Render(ui.ActivitySymbol(cat))
		lines = append(lines, fmt.Sprintf("%s %s %s — waiting for the first structured event", symbol, w.TicketID, ui.ActivityStyle(cat).Render(strings.ToUpper(state))))
	}
	if d.Orchestration != nil && d.Orchestration.HeartbeatCount > 0 {
		lines = append(lines, fmt.Sprintf("heartbeat count: %d", d.Orchestration.HeartbeatCount))
	}
	return strings.Join(lines, "\n")
}

func analysisPhaseLabel(phase string) string {
	switch strings.ToLower(phase) {
	case "model_requested":
		return "Waiting for model…"
	case "starting":
		return "Starting analysis…"
	default:
		return strings.ReplaceAll(phase, "_", " ")
	}
}

func renderAnalysisStatus(analysis *client.AnalysisStatus) string {
	if analysis == nil {
		return ""
	}
	parts := make([]string, 0, 4)
	if analysis.TicketID != "" {
		parts = append(parts, analysis.TicketID)
	}
	parts = append(parts, analysisPhaseLabel(analysis.Phase))
	if analysis.Executor != "" {
		parts = append(parts, "executor="+analysis.Executor)
	}
	if analysis.Model != "" {
		parts = append(parts, "model="+analysis.Model)
	}
	return strings.Join(parts, " — ")
}

// renderActivitySection renders the default structured Activity pane: safe
// progress events only, or the bounded lifecycle fallback when none
// are recorded yet.
func (rm *RunModel) renderActivitySection(width int) string {
	var b strings.Builder
	b.WriteString(ui.LabelStyle.Render("Activity"))
	b.WriteString("\n")
	switch {
	case rm.activityErr != "":
		b.WriteString(ui.LabelStyle.Render("Events error:") + " " + rm.activityErr)
		b.WriteString("\n")
	case len(rm.activityEvents) > 0:
		for _, ev := range rm.activityEvents {
			b.WriteString(ui.WrapText(formatActivityEvent(ev), width))
			b.WriteString("\n")
		}
	case rm.activityLoaded:
		b.WriteString(rm.lifecycleFallback())
		b.WriteString("\n")
	default:
		b.WriteString("(loading activity…)\n")
	}
	return strings.TrimRight(b.String(), "\n")
}

// renderAuditSection renders the explicit, paginated Raw Audit Log pane:
// raw transcript/protocol-shaped messages retained for diagnosis, plus a
// live tail while the current run is being tailed. This is the only place
// raw executor output is ever rendered.
func (rm *RunModel) renderAuditSection(width int) string {
	var b strings.Builder
	b.WriteString(ui.LabelStyle.Render("Raw Audit Log"))
	b.WriteString("\n")
	switch {
	case rm.auditLoading:
		b.WriteString("Loading…\n")
	case rm.auditErr != "":
		b.WriteString(ui.LabelStyle.Render("Audit log error:") + " " + rm.auditErr)
		b.WriteString("\n")
	case rm.auditLoaded && len(rm.auditEvents) > 0:
		for _, ev := range rm.auditEvents {
			if ev.Message == "" {
				continue
			}
			b.WriteString(formatAuditEvent(ev, width))
			b.WriteString("\n")
		}
		// Page position and n/N page are pinned in the footer instead of
		// repeated here, so they stay visible without scrolling down; see
		// footerKeys in app/view.go.
	case rm.auditLoaded:
		b.WriteString("(no raw log events)\n")
	default:
		b.WriteString("(loading…)\n")
	}
	if rm.streamErr != "" {
		b.WriteString("\n")
		b.WriteString(ui.LabelStyle.Render("Stream error:") + " " + rm.streamErr)
		b.WriteString("\n")
	}
	if len(rm.logLines) > 0 || len(rm.historyLines) > 0 || rm.historyLoading || rm.historyErr != "" {
		b.WriteString("\n")
		b.WriteString(ui.LabelStyle.Render("Live Tail:"))
		b.WriteString("\n")
		if statusLine := rm.renderHistoryStatusLine(); statusLine != "" {
			b.WriteString(statusLine)
		}
		for i, line := range rm.historyLines {
			level := ""
			if i < len(rm.historyLineLevels) {
				level = rm.historyLineLevels[i]
			}
			style := ""
			if i < len(rm.historyLineStyles) {
				style = rm.historyLineStyles[i]
			}
			b.WriteString(formatAuditEvent(client.LogEvent{Message: line, Level: level, Style: style}, width))
			b.WriteString("\n")
		}
		for i, line := range rm.logLines {
			level := ""
			if i < len(rm.logLineLevels) {
				level = rm.logLineLevels[i]
			}
			style := ""
			if i < len(rm.logLineStyles) {
				style = rm.logLineStyles[i]
			}
			b.WriteString(formatAuditEvent(client.LogEvent{Message: line, Level: level, Style: style}, width))
			b.WriteString("\n")
		}
	}
	return strings.TrimRight(b.String(), "\n")
}

// formatAuditEvent decorates a redacted raw audit message using API-provided
// display metadata. The message is still JSON-formatted and wrapped exactly
// as before, while CopyText intentionally continues to return only the raw
// message.
func formatAuditEvent(ev client.LogEvent, width int) string {
	const prefixWidth = 2 // symbol plus following space
	messageWidth := width - prefixWidth
	if messageWidth < 1 {
		messageWidth = 1
	}
	style := auditEventStyle(ev)
	prefix := style.Render(ui.AuditSymbol(ev.Level) + " ")
	return prefix + style.Render(ui.WrapJSONAware(ev.Message, messageWidth))
}

// auditEventStyle applies the backend's shared log presentation token. Level
// remains the fallback for live/older events that predate the style field.
func auditEventStyle(ev client.LogEvent) lipgloss.Style {
	switch ev.Style {
	case "bold red":
		return lipgloss.NewStyle().Bold(true).Foreground(ui.ColorError)
	case "red":
		return lipgloss.NewStyle().Foreground(ui.ColorError)
	case "yellow":
		return lipgloss.NewStyle().Foreground(ui.ColorWarning)
	case "green":
		return lipgloss.NewStyle().Foreground(ui.ColorSuccess)
	case "magenta":
		return lipgloss.NewStyle().Foreground(ui.ColorSecondary)
	case "bold blue":
		return lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("4"))
	case "cyan":
		return lipgloss.NewStyle().Foreground(ui.ColorPrimary)
	case "dim":
		return lipgloss.NewStyle().Foreground(ui.ColorNeutral)
	default:
		return ui.AuditStyle(ev.Level)
	}
}

// Render renders the run screen as plain text sized to width.
func (rm *RunModel) Render(width int) string {
	d := rm.data
	var b strings.Builder

	if d == nil || d.RunID == "" {
		rwsSection := ""
		if d != nil {
			rwsSection = renderResumeWatchSection(d.ResumeWatchStatus, lastCooldownOf(d))
		}
		if rm.streamErr != "" {
			b.WriteString("No active orchestration run.\n\n" + ui.LabelStyle.Render("Stream error:") + " " + rm.streamErr)
			if rwsSection != "" {
				b.WriteString("\n\n" + rwsSection)
			}
		} else if rwsSection != "" {
			b.WriteString("No active orchestration run.\n\n" + rwsSection)
		} else {
			b.WriteString("No active orchestration run.")
		}
		return strings.TrimRight(b.String(), "\n")
	}

	fmt.Fprintf(&b, "%s  %s\n", d.RunID, strings.ToUpper(d.Status))
	if d.StartedAtISO != "" {
		fmt.Fprintf(&b, "%s %s\n", ui.LabelStyle.Render("Started:"), ui.FormatLocalTS(d.StartedAtISO))
	}

	b.WriteString("\n")
	b.WriteString(renderWorkersSection(d, width))

	if outcomes := rm.renderLiveOutcomesSection(width); outcomes != "" {
		b.WriteString("\n")
		b.WriteString(outcomes)
		b.WriteString("\n")
	}

	if analysis := renderAnalysisStatus(d.Analysis); analysis != "" {
		b.WriteString("\n")
		b.WriteString(ui.LabelStyle.Render("Analysis"))
		b.WriteString("\n")
		b.WriteString(analysis)
		b.WriteString("\n")
	}

	b.WriteString("\n")
	if rm.mode == RunModeAudit {
		b.WriteString(rm.renderAuditSection(width))
	} else {
		b.WriteString(rm.renderActivitySection(width))
	}

	if rwsSection := renderResumeWatchSection(d.ResumeWatchStatus, lastCooldownOf(d)); rwsSection != "" {
		b.WriteString("\n")
		b.WriteString(rwsSection)
	}

	return strings.TrimRight(b.String(), "\n")
}
