package app

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
	"lanegate/tui/internal/client"
	"lanegate/tui/internal/screens"
)

// fixturesRootForTest resolves the shared Python/Go fixture corpus at
// tests/fixtures/tui_contracts, relative to this source file rather than the
// test binary's working directory.
func fixturesRootForTest(t *testing.T) string {
	t.Helper()
	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("could not resolve caller for fixturesRootForTest")
	}
	// this file: tui/internal/app/model_test.go
	root := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "tests", "fixtures", "tui_contracts")
	if _, err := os.Stat(root); err != nil {
		t.Fatalf("fixtures root not found at %s: %v", root, err)
	}
	return root
}

// newTestClient builds a real FixtureClient against the shared fixture
// corpus so Model construction exercises a genuine client.Client rather than
// a hand-rolled fake.
func newTestClient(t *testing.T) client.Client {
	t.Helper()
	c, err := client.NewFixtureClient(fixturesRootForTest(t))
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}
	return c
}

func TestNew_StartsNotReady(t *testing.T) {
	m := New(newTestClient(t))
	if m.ready {
		t.Error("expected a freshly constructed model to not be ready before the first WindowSizeMsg")
	}
}

// TestInit_TriggersInitialBoardLoad documents the routing behavior this
// ticket adds: the app must not sit idle after startup waiting for a key
// press (that was the one-shot-print-shaped gap the ticket's close criteria
// called out). Init() now kicks off an async Board fetch instead of
// returning nil, using the standard Bubble Tea "Cmd closure returns a typed
// Msg" pattern.
func TestInit_TriggersInitialBoardLoad(t *testing.T) {
	m := New(newTestClient(t))

	cmd := m.Init()
	if cmd == nil {
		t.Fatal("Init() cmd = nil, want a board-load command")
	}
	if !m.loading {
		t.Error("expected Init() to mark the model as loading")
	}

	msg := cmd()
	if _, ok := msg.(boardLoadedMsg); !ok {
		t.Fatalf("Init() cmd() = %T, want boardLoadedMsg", msg)
	}
}

func TestView_BeforeReady(t *testing.T) {
	m := New(newTestClient(t))
	if got := m.View(); got != "Loading..." {
		t.Errorf("View() before WindowSizeMsg = %q, want %q", got, "Loading...")
	}
}

func TestUpdate_WindowSizeMsg_MarksReady(t *testing.T) {
	m := New(newTestClient(t))

	updated, cmd := m.Update(tea.WindowSizeMsg{Width: 80, Height: 24})
	if cmd != nil {
		t.Errorf("Update(WindowSizeMsg) cmd = %v, want nil", cmd)
	}

	got, ok := updated.(*Model)
	if !ok {
		t.Fatalf("Update returned %T, want *Model", updated)
	}
	if !got.ready {
		t.Error("expected model to be ready after WindowSizeMsg")
	}
	if got.width != 80 || got.height != 24 {
		t.Errorf("width/height = %d/%d, want 80/24", got.width, got.height)
	}
}

func TestView_AfterReady(t *testing.T) {
	m := New(newTestClient(t))
	m.Update(tea.WindowSizeMsg{Width: 80, Height: 24})

	got := m.View()
	if got == "Loading..." {
		t.Error("expected View() to change once the model is ready")
	}
	if got == "" {
		t.Error("expected a non-empty view once ready")
	}
}

func TestUpdate_QuitKeys(t *testing.T) {
	tests := []struct {
		name string
		msg  tea.KeyMsg
	}{
		{name: "q", msg: tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("q")}},
		{name: "ctrl+c", msg: tea.KeyMsg{Type: tea.KeyCtrlC}},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			m := New(newTestClient(t))
			_, cmd := m.Update(tt.msg)

			if cmd == nil {
				t.Fatalf("Update(%q) cmd = nil, want tea.Quit", tt.name)
			}
			msgOut := cmd()
			if _, ok := msgOut.(tea.QuitMsg); !ok {
				t.Errorf("Update(%q) cmd() = %T, want tea.QuitMsg", tt.name, msgOut)
			}
		})
	}
}

func TestUpdate_NonQuitKey_DoesNotQuit(t *testing.T) {
	m := New(newTestClient(t))
	_, cmd := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("j")})
	if cmd != nil {
		t.Errorf("Update(%q) cmd = %v, want nil", "j", cmd)
	}
}

func TestUpdate_UnhandledMsgType_ReturnsModelUnchanged(t *testing.T) {
	m := New(newTestClient(t))
	updated, cmd := m.Update(struct{}{})
	if cmd != nil {
		t.Errorf("Update(unknown msg) cmd = %v, want nil", cmd)
	}
	if updated != m {
		t.Error("Update(unknown msg) should return the same model instance")
	}
}

// --- Screen routing ---
//
// fakeClient is a hand-rolled client.Client used to assert *which* method
// the router calls when switching screens, and to control success/error
// outcomes deterministically without touching the filesystem or network.

type fakeClient struct {
	boardCalls, ticketCalls, blockedCalls int
	lastTicketID                          string

	board   *client.BoardPayload
	ticket  *client.TicketDetail
	blocked *client.BlockedPayload

	boardErr, ticketErr, blockedErr error

	diffCalls      int
	lastDiffTicket string
	diff           *client.DiffPayload
	diffErr        error

	runCalls          int
	run               *client.RunPayload
	runErr            error
	runHistory        *client.RunHistoryPayload
	runHistoryCalls   int
	runEventsCalls    int
	lastRunEventsID   string
	runEvents         *client.RunEventsPayload
	runEventsErr      error
	runLogsCalls      int
	lastRunLogsID     string
	lastRunLogsOffset int
	runLogs           *client.RunLogsPayload
	runLogsErr        error

	runSummaryCalls  int
	lastRunSummaryID string
	runSummary       *client.RunSummaryPayload
	runSummaryErr    error

	settingsCalls int
	settings      *client.SettingsPayload
	settingsErr   error

	poolsCalls int
	pools      *client.PoolsPayload
	poolsErr   error

	updatePoolExecutorsCalls    int
	lastUpdatePoolExecutorsName string
	lastUpdatePoolExecutorsList []string
	updatePoolExecutorsResult   *client.Pool
	updatePoolExecutorsErr      error

	// runLogStreamBodies supplies one text/event-stream body per
	// OpenRunLogStream call, in order; calls past the end of the slice repeat
	// the last body (simulating a server that keeps replaying its final
	// state). runLogStreamErr, if set, is returned instead on every call.
	runLogStreamCalls  int
	runLogStreamBodies []string
	runLogStreamErr    error

	// logPageCalls records every GetRunLogPage(offset, limit) call, in order,
	// so tests can assert what history page(s) were requested. logPageResult
	// (or logPageErr) is returned for every call.
	logPageCalls  []logPageCall
	logPageResult *client.LogPagePayload
	logPageErr    error
}

type logPageCall struct {
	offset, limit int
}

func (f *fakeClient) GetBoard(ctx context.Context) (*client.BoardPayload, error) {
	f.boardCalls++
	return f.board, f.boardErr
}

func (f *fakeClient) GetTickets(ctx context.Context) (*client.TicketsPayload, error) {
	return &client.TicketsPayload{}, nil
}

func (f *fakeClient) GetTicketDetail(ctx context.Context, ticketID string) (*client.TicketDetail, error) {
	f.ticketCalls++
	f.lastTicketID = ticketID
	return f.ticket, f.ticketErr
}

func (f *fakeClient) GetBlocked(ctx context.Context) (*client.BlockedPayload, error) {
	f.blockedCalls++
	return f.blocked, f.blockedErr
}

func (f *fakeClient) GetDiff(ctx context.Context, ticketID string) (*client.DiffPayload, error) {
	f.diffCalls++
	f.lastDiffTicket = ticketID
	return f.diff, f.diffErr
}

func (f *fakeClient) GetCurrentRun(ctx context.Context) (*client.RunPayload, error) {
	f.runCalls++
	return f.run, f.runErr
}

func (f *fakeClient) GetSettings(ctx context.Context) (*client.SettingsPayload, error) {
	f.settingsCalls++
	return f.settings, f.settingsErr
}

func (f *fakeClient) GetPools(ctx context.Context) (*client.PoolsPayload, error) {
	f.poolsCalls++
	return f.pools, f.poolsErr
}

func (f *fakeClient) UpdatePoolExecutors(ctx context.Context, poolName string, executors []string) (*client.Pool, error) {
	f.updatePoolExecutorsCalls++
	f.lastUpdatePoolExecutorsName = poolName
	f.lastUpdatePoolExecutorsList = executors
	return f.updatePoolExecutorsResult, f.updatePoolExecutorsErr
}

func (f *fakeClient) OpenRunLogStream(ctx context.Context, lastEventID string) (io.ReadCloser, error) {
	f.runLogStreamCalls++
	if f.runLogStreamErr != nil {
		return nil, f.runLogStreamErr
	}
	body := ""
	if n := len(f.runLogStreamBodies); n > 0 {
		idx := f.runLogStreamCalls - 1
		if idx >= n {
			idx = n - 1
		}
		body = f.runLogStreamBodies[idx]
	}
	return io.NopCloser(strings.NewReader(body)), nil
}

func (f *fakeClient) GetRunLogPage(ctx context.Context, offset, limit int) (*client.LogPagePayload, error) {
	f.logPageCalls = append(f.logPageCalls, logPageCall{offset: offset, limit: limit})
	return f.logPageResult, f.logPageErr
}

func (f *fakeClient) GetRunHistory(ctx context.Context) (*client.RunHistoryPayload, error) {
	f.runHistoryCalls++
	if f.runHistory != nil {
		return f.runHistory, nil
	}
	return &client.RunHistoryPayload{Runs: []client.RunSummaryPayload{}}, nil
}

func (f *fakeClient) GetRunSummary(ctx context.Context, runID string) (*client.RunSummaryPayload, error) {
	f.runSummaryCalls++
	f.lastRunSummaryID = runID
	if f.runSummary != nil {
		return f.runSummary, f.runSummaryErr
	}
	return &client.RunSummaryPayload{RunID: runID}, f.runSummaryErr
}

func (f *fakeClient) GetRunLogs(ctx context.Context, runID string, offset, limit int) (*client.RunLogsPayload, error) {
	f.runLogsCalls++
	f.lastRunLogsID = runID
	f.lastRunLogsOffset = offset
	if f.runLogs != nil {
		return f.runLogs, f.runLogsErr
	}
	return &client.RunLogsPayload{RunID: runID, Events: []client.LogEvent{}, TotalCount: 0, Offset: offset, Limit: limit}, f.runLogsErr
}

func (f *fakeClient) GetRunEvents(ctx context.Context, runID string) (*client.RunEventsPayload, error) {
	f.runEventsCalls++
	f.lastRunEventsID = runID
	if f.runEvents != nil {
		return f.runEvents, f.runEventsErr
	}
	return &client.RunEventsPayload{RunID: runID, Events: []client.ExecutorEvent{}}, f.runEventsErr
}

// readyModel builds a Model, marks it ready (as the real program does via
// the first WindowSizeMsg), and seeds it with a loaded Board so
// selectedTicketID is populated for tests that switch to the Ticket screen.
func readyModel(t *testing.T, fc *fakeClient) *Model {
	t.Helper()
	m := New(fc)
	updated, _ := m.Update(tea.WindowSizeMsg{Width: 80, Height: 24})
	m = updated.(*Model)
	updated, _ = m.Update(boardLoadedMsg{data: fc.board})
	return updated.(*Model)
}

func key(s string) tea.KeyMsg {
	switch s {
	case "esc":
		return tea.KeyMsg{Type: tea.KeyEsc}
	case "enter":
		return tea.KeyMsg{Type: tea.KeyEnter}
	case "up":
		return tea.KeyMsg{Type: tea.KeyUp}
	case "down":
		return tea.KeyMsg{Type: tea.KeyDown}
	case "pgup":
		return tea.KeyMsg{Type: tea.KeyPgUp}
	case "pgdown":
		return tea.KeyMsg{Type: tea.KeyPgDown}
	case "home":
		return tea.KeyMsg{Type: tea.KeyHome}
	case "end":
		return tea.KeyMsg{Type: tea.KeyEnd}
	case "tab":
		return tea.KeyMsg{Type: tea.KeyTab}
	case "shift+tab":
		return tea.KeyMsg{Type: tea.KeyShiftTab}
	default:
		return tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(s)}
	}
}

func TestUpdate_ScreenSwitch_LoadsTicketDetailForSelectedTicket(t *testing.T) {
	fc := &fakeClient{
		board: &client.BoardPayload{
			Tickets: map[string][]client.Ticket{
				"open": {{ID: "TICK-100", Title: "First"}},
			},
		},
		ticket: &client.TicketDetail{ID: "TICK-100", Title: "First"},
	}
	m := readyModel(t, fc)
	if m.screen != screenBoard {
		t.Fatalf("screen = %v, want screenBoard", m.screen)
	}
	if m.selectedTicketID != "TICK-100" {
		t.Fatalf("selectedTicketID = %q, want TICK-100 (seeded from board data)", m.selectedTicketID)
	}

	updated, cmd := m.Update(key("2"))
	m = updated.(*Model)
	if m.screen != screenTicket {
		t.Errorf("screen after pressing 2 = %v, want screenTicket", m.screen)
	}
	if cmd == nil {
		t.Fatal("Update(2) cmd = nil, want a ticket-load command")
	}

	msg := cmd()
	tl, ok := msg.(ticketLoadedMsg)
	if !ok {
		t.Fatalf("Update(2) cmd() = %T, want ticketLoadedMsg", msg)
	}
	if fc.ticketCalls != 1 {
		t.Errorf("GetTicketDetail calls = %d, want 1", fc.ticketCalls)
	}
	if fc.lastTicketID != "TICK-100" {
		t.Errorf("GetTicketDetail called with %q, want TICK-100", fc.lastTicketID)
	}

	updated, _ = m.Update(tl)
	m = updated.(*Model)
	if got := m.ticket.GetData(); got == nil || got.ID != "TICK-100" {
		t.Errorf("ticket screen data = %+v, want TICK-100 detail applied via SetData", got)
	}
	if m.loading {
		t.Error("expected loading to clear once ticketLoadedMsg is processed for the active screen")
	}
}

func TestUpdate_EnterOnBoard_OpensTicketDetailForSelectedRow(t *testing.T) {
	fc := &fakeClient{
		board: &client.BoardPayload{
			Tickets: map[string][]client.Ticket{
				"open": {{ID: "TICK-100", Title: "First"}},
			},
		},
		ticket: &client.TicketDetail{ID: "TICK-100", Title: "First"},
	}
	m := readyModel(t, fc)
	if m.screen != screenBoard {
		t.Fatalf("screen = %v, want screenBoard", m.screen)
	}

	updated, cmd := m.Update(key("enter"))
	m = updated.(*Model)
	if m.screen != screenTicket {
		t.Fatalf("screen after enter = %v, want screenTicket", m.screen)
	}
	if cmd == nil {
		t.Fatal("expected a load command for the ticket detail screen")
	}
}

func TestUpdate_EnterOnBoard_NoSelectionIsNoop(t *testing.T) {
	fc := &fakeClient{board: &client.BoardPayload{}}
	m := readyModel(t, fc)

	updated, _ := m.Update(key("enter"))
	m = updated.(*Model)
	if m.screen != screenBoard {
		t.Fatalf("screen after enter with no selection = %v, want screenBoard (unchanged)", m.screen)
	}
}

func TestUpdate_ScreenSwitch_ToBlocked_CallsGetBlocked(t *testing.T) {
	fc := &fakeClient{
		board:   &client.BoardPayload{},
		blocked: &client.BlockedPayload{Blocked: []client.BlockedTicket{{ID: "TICK-9", Title: "Stuck"}}},
	}
	m := readyModel(t, fc)

	updated, cmd := m.Update(key("3"))
	m = updated.(*Model)
	if m.screen != screenBlocked {
		t.Fatalf("screen after pressing 3 = %v, want screenBlocked", m.screen)
	}
	msg := cmd()
	bl, ok := msg.(blockedLoadedMsg)
	if !ok {
		t.Fatalf("Update(3) cmd() = %T, want blockedLoadedMsg", msg)
	}
	if fc.blockedCalls != 1 {
		t.Errorf("GetBlocked calls = %d, want 1", fc.blockedCalls)
	}

	updated, _ = m.Update(bl)
	m = updated.(*Model)
	if got := m.blocked.GetData(); got == nil || len(got.Blocked) != 1 {
		t.Errorf("blocked screen data = %+v, want the fake's one blocked ticket applied", got)
	}
}

func TestUpdate_AllSevenScreensReachableViaNumberKeys(t *testing.T) {
	fc := &fakeClient{
		board:    &client.BoardPayload{},
		diff:     &client.DiffPayload{},
		run:      &client.RunPayload{},
		settings: &client.SettingsPayload{},
	}
	m := readyModel(t, fc)

	want := []screenID{screenBoard, screenTicket, screenBlocked, screenDiff, screenRun, screenHistory, screenSettings}
	for i, k := range []string{"1", "2", "3", "4", "5", "6", "7"} {
		updated, _ := m.Update(key(k))
		m = updated.(*Model)
		if m.screen != want[i] {
			t.Errorf("after pressing %q, screen = %v, want %v", k, m.screen, want[i])
		}
	}
}

func TestUpdate_BoardSelectionKeysUpdateSelectedTicket(t *testing.T) {
	fc := &fakeClient{
		board: &client.BoardPayload{
			Tickets: map[string][]client.Ticket{
				"open": {
					{ID: "TICK-100", Title: "First"},
					{ID: "TICK-101", Title: "Second"},
				},
			},
		},
	}
	m := readyModel(t, fc)
	if m.selectedTicketID != "TICK-100" {
		t.Fatalf("selectedTicketID = %q, want TICK-100", m.selectedTicketID)
	}

	updated, cmd := m.Update(key("j"))
	m = updated.(*Model)
	if cmd != nil {
		t.Errorf("Update(j) cmd = %v, want nil", cmd)
	}
	if m.selectedTicketID != "TICK-101" {
		t.Errorf("selectedTicketID after j = %q, want TICK-101", m.selectedTicketID)
	}

	updated, _ = m.Update(key("k"))
	m = updated.(*Model)
	if m.selectedTicketID != "TICK-100" {
		t.Errorf("selectedTicketID after k = %q, want TICK-100", m.selectedTicketID)
	}
}

func TestUpdate_HelpToggleAndEscClose(t *testing.T) {
	fc := &fakeClient{board: &client.BoardPayload{}}
	m := readyModel(t, fc)

	updated, cmd := m.Update(key("?"))
	m = updated.(*Model)
	if cmd != nil {
		t.Errorf("Update(?) cmd = %v, want nil", cmd)
	}
	if !m.helpVisible {
		t.Fatal("expected ? to show help")
	}
	view := m.View()
	for _, want := range []string{"Global", "1-7", "q, ctrl+c", "pgup/pgdn"} {
		if !strings.Contains(view, want) {
			t.Errorf("help view missing %q:\n%s", want, view)
		}
	}

	updated, cmd = m.Update(key("esc"))
	m = updated.(*Model)
	if cmd != nil {
		t.Errorf("esc while help is visible: cmd = %v, want nil", cmd)
	}
	if m.helpVisible {
		t.Error("expected esc to close help")
	}
	if m.screen != screenBoard {
		t.Errorf("esc while help is visible changed screen to %v, want screenBoard", m.screen)
	}
}

func TestUpdate_ScreenSwitch_ToDiff_CallsGetDiff(t *testing.T) {
	fc := &fakeClient{
		board: &client.BoardPayload{
			Tickets: map[string][]client.Ticket{"open": {{ID: "TICK-100", Title: "First"}}},
		},
		diff: &client.DiffPayload{ID: "TICK-100"},
	}
	m := readyModel(t, fc)

	updated, cmd := m.Update(key("4"))
	m = updated.(*Model)
	if m.screen != screenDiff {
		t.Fatalf("screen after pressing 4 = %v, want screenDiff", m.screen)
	}
	if cmd == nil {
		t.Fatal("Update(4) cmd = nil, want a diff-load command")
	}

	msg := cmd()
	dl, ok := msg.(diffLoadedMsg)
	if !ok {
		t.Fatalf("Update(4) cmd() = %T, want diffLoadedMsg", msg)
	}
	if fc.diffCalls != 1 {
		t.Errorf("GetDiff calls = %d, want 1", fc.diffCalls)
	}
	if fc.lastDiffTicket != "TICK-100" {
		t.Errorf("GetDiff called with %q, want TICK-100", fc.lastDiffTicket)
	}

	updated, _ = m.Update(dl)
	m = updated.(*Model)
	if got := m.diff.GetData(); got == nil || got.ID != "TICK-100" {
		t.Errorf("diff screen data = %+v, want TICK-100 detail applied via SetData", got)
	}
}

func TestUpdate_DiffPageScrollChangesRenderedWindow(t *testing.T) {
	var patch strings.Builder
	for i := 0; i < 30; i++ {
		patch.WriteString("+line ")
		patch.WriteString(string(rune('a' + i%26)))
		patch.WriteString("\n")
	}
	fc := &fakeClient{
		board: &client.BoardPayload{
			Tickets: map[string][]client.Ticket{"open": {{ID: "TICK-100", Title: "First"}}},
		},
		diff: &client.DiffPayload{
			ID:     "TICK-100",
			Base:   "main",
			Branch: "tick-100",
			Files: []client.DiffFile{{
				Path:   "long.txt",
				Status: "M",
				Patch:  patch.String(),
			}},
		},
	}
	m := readyModel(t, fc)
	updated, cmd := m.Update(key("4"))
	m = updated.(*Model)
	updated, _ = m.Update(cmd())
	m = updated.(*Model)
	updated, _ = m.Update(tea.WindowSizeMsg{Width: 48, Height: 8})
	m = updated.(*Model)

	before := m.View()
	updated, cmd = m.Update(key("pgdown"))
	m = updated.(*Model)
	if cmd != nil {
		t.Errorf("Update(pgdown) cmd = %v, want nil", cmd)
	}
	after := m.View()
	if before == after {
		t.Fatalf("expected pgdown to change the rendered diff window:\n%s", after)
	}
	if !strings.Contains(after, "+line") {
		t.Errorf("scrolled diff view lost patch content:\n%s", after)
	}

	updated, _ = m.Update(key("home"))
	m = updated.(*Model)
	if got := m.View(); got != before {
		t.Errorf("home did not return to the top of the diff view\n--- got ---\n%s\n--- want ---\n%s", got, before)
	}
}

func TestUpdate_ScreenSwitch_ToSettings_CallsGetSettings(t *testing.T) {
	fc := &fakeClient{
		board:    &client.BoardPayload{},
		settings: &client.SettingsPayload{RepoRoot: "/repo"},
		pools:    &client.PoolsPayload{Pools: []client.Pool{{Name: "default", Executors: []string{"claude-1"}}}},
	}
	m := readyModel(t, fc)

	updated, cmd := m.Update(key("7"))
	m = updated.(*Model)
	if m.screen != screenSettings {
		t.Fatalf("screen after pressing 7 = %v, want screenSettings", m.screen)
	}
	msg := cmd()
	batch, ok := msg.(tea.BatchMsg)
	if !ok {
		t.Fatalf("Update(7) cmd() = %T, want tea.BatchMsg (settings + pools)", msg)
	}

	var sawSettings, sawPools bool
	for _, c := range batch {
		out := c()
		updated, _ := m.Update(out)
		m = updated.(*Model)
		switch out.(type) {
		case settingsLoadedMsg:
			sawSettings = true
		case poolsLoadedMsg:
			sawPools = true
		}
	}
	if !sawSettings {
		t.Fatal("expected the Settings switch batch to include a settingsLoadedMsg")
	}
	if !sawPools {
		t.Fatal("expected the Settings switch batch to include a poolsLoadedMsg")
	}
	if fc.settingsCalls != 1 {
		t.Errorf("GetSettings calls = %d, want 1", fc.settingsCalls)
	}
	if fc.poolsCalls != 1 {
		t.Errorf("GetPools calls = %d, want 1", fc.poolsCalls)
	}

	if got := m.settings.GetData(); got == nil || got.RepoRoot != "/repo" {
		t.Errorf("settings screen data = %+v, want /repo applied via SetData", got)
	}
	if got := m.settings.GetPools(); len(got) != 1 || got[0].Name != "default" {
		t.Errorf("settings screen pools = %+v, want [default]", got)
	}
}

// runBatchCmds executes every Cmd in a tea.BatchMsg produced by switching to
// the Run screen (GetCurrentRun + the first run-log stream read), feeding
// each resulting message back through Update, and returns the run-log Cmd
// (if any) so the caller can keep draining the self-reissuing stream.
func runBatchCmds(t *testing.T, m *Model, cmd tea.Cmd) (*Model, tea.Cmd) {
	t.Helper()
	msg := cmd()
	batch, ok := msg.(tea.BatchMsg)
	if !ok {
		t.Fatalf("switching to the Run screen: cmd() = %T, want tea.BatchMsg", msg)
	}

	var logCmd tea.Cmd
	for _, c := range batch {
		out := c()
		updated, next := m.Update(out)
		m = updated.(*Model)
		if _, ok := out.(runLogMsg); ok {
			logCmd = next
		}
	}
	return m, logCmd
}

func TestUpdate_ScreenSwitch_ToRun_LoadsActivityWithoutStartingRawStream(t *testing.T) {
	fc := &fakeClient{
		board:     &client.BoardPayload{},
		run:       &client.RunPayload{RunID: "run-1", Status: "running"},
		runEvents: &client.RunEventsPayload{Events: []client.ExecutorEvent{{TicketID: "TICK-1", Progress: client.ExecutorProgress{Activity: "planning"}}}},
	}
	m := readyModel(t, fc)

	updated, cmd := m.Update(key("5"))
	m = updated.(*Model)
	if m.screen != screenRun {
		t.Fatalf("screen after pressing 5 = %v, want screenRun", m.screen)
	}
	if cmd == nil {
		t.Fatal("Update(5) cmd = nil, want a batch of run-load + activity commands")
	}

	m, _ = runBatchCmds(t, m, cmd)

	if fc.runCalls != 1 {
		t.Errorf("GetCurrentRun calls = %d, want 1", fc.runCalls)
	}
	if fc.runEventsCalls != 1 || fc.lastRunEventsID != "" {
		t.Errorf("GetRunEvents calls/id = %d/%q, want 1/live current", fc.runEventsCalls, fc.lastRunEventsID)
	}
	if fc.runLogStreamCalls != 0 {
		t.Errorf("OpenRunLogStream calls = %d, want 0 in default Activity", fc.runLogStreamCalls)
	}
	if got := m.run.GetData(); got == nil || got.RunID != "run-1" {
		t.Errorf("run screen data = %+v, want run-1 applied via SetData", got)
	}
	if events := m.run.ActivityEvents(); len(events) != 1 || events[0].TicketID != "TICK-1" {
		t.Errorf("activity events = %+v, want TICK-1 structured event", events)
	}
	if !m.runActivityPolling {
		t.Error("default Activity should start its live refresh poller after loading events")
	}
}

// TestUpdate_RunActivityPollLoadsRunSummaryIntoLiveBatchTickets is a
// regression test: the Activity poll must also fetch the run summary and
// feed its BatchTickets into RunModel's live outcome table, on the same 2s
// cadence as the rest of the Run screen's live refresh.
func TestUpdate_RunActivityPollLoadsRunSummaryIntoLiveBatchTickets(t *testing.T) {
	fc := &fakeClient{
		board:     &client.BoardPayload{},
		run:       &client.RunPayload{RunID: "run-1", Status: "running"},
		runEvents: &client.RunEventsPayload{Events: []client.ExecutorEvent{{TicketID: "TICK-1", Progress: client.ExecutorProgress{Activity: "planning"}}}},
	}
	m := readyModel(t, fc)
	updated, cmd := m.Update(key("5"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)

	fc.runSummary = &client.RunSummaryPayload{
		RunID: "run-1",
		BatchTickets: []client.TicketOutcome{
			{TicketID: "TICK-1", Executor: "claude-a", Outcome: "success", DurationSeconds: 12.5},
		},
	}

	updated, cmd = m.Update(runActivityPollMsg{gen: m.runActivityPollGen})
	m = updated.(*Model)
	if cmd == nil {
		t.Fatal("Activity poll should schedule a refresh")
	}
	batch, ok := cmd().(tea.BatchMsg)
	if !ok {
		t.Fatalf("Activity poll cmd() = %T, want tea.BatchMsg", cmd())
	}
	for _, c := range batch[:len(batch)-1] { // Skip the timer reschedule command.
		updated, _ = m.Update(c())
		m = updated.(*Model)
	}

	if fc.runSummaryCalls != 1 || fc.lastRunSummaryID != "run-1" {
		t.Errorf("GetRunSummary calls/id = %d/%q, want 1/%q", fc.runSummaryCalls, fc.lastRunSummaryID, "run-1")
	}
	got := m.run.LiveBatchTickets()
	if len(got) != 1 || got[0].TicketID != "TICK-1" || got[0].Outcome != "success" {
		t.Errorf("LiveBatchTickets() = %+v, want one success outcome for TICK-1", got)
	}
}

// TestUpdate_RunSummaryLoadedMsg_DropsOutOfOrderResponse guards against a
// slower, older run-summary request (GetRunSummary enriches every
// non-success outcome from disk, so its latency varies) completing after a
// newer one and clobbering the Live Outcomes table with stale per-ticket
// outcomes.
func TestUpdate_RunSummaryLoadedMsg_DropsOutOfOrderResponse(t *testing.T) {
	fc := &fakeClient{board: &client.BoardPayload{}}
	m := readyModel(t, fc)

	newer := runSummaryLoadedMsg{
		runID: "run-1",
		gen:   2,
		data: &client.RunSummaryPayload{
			RunID:        "run-1",
			BatchTickets: []client.TicketOutcome{{TicketID: "TICK-2", Outcome: "success"}},
		},
	}
	older := runSummaryLoadedMsg{
		runID: "run-1",
		gen:   1,
		data: &client.RunSummaryPayload{
			RunID:        "run-1",
			BatchTickets: []client.TicketOutcome{{TicketID: "TICK-1", Outcome: "changes_requested"}},
		},
	}

	updated, _ := m.Update(newer)
	m = updated.(*Model)
	updated, _ = m.Update(older) // arrives late; must not overwrite the newer table
	m = updated.(*Model)

	got := m.run.LiveBatchTickets()
	if len(got) != 1 || got[0].TicketID != "TICK-2" {
		t.Errorf("LiveBatchTickets() = %+v, want the newer gen's TICK-2 to survive the late older response", got)
	}
}

func TestUpdate_RunActivityPoll_RetainsSnapshotAfterRefreshError(t *testing.T) {
	fc := &fakeClient{
		board:     &client.BoardPayload{},
		run:       &client.RunPayload{RunID: "run-1", Status: "running"},
		runEvents: &client.RunEventsPayload{},
	}
	m := readyModel(t, fc)
	updated, cmd := m.Update(key("5"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)

	refreshErr := errors.New("connection reset")
	updated, _ = m.Update(runLoadedMsg{err: refreshErr, autoRefreshing: true, gen: m.runSnapshotReqGen})
	m = updated.(*Model)
	if got := m.run.GetData(); got == nil || got.RunID != "run-1" {
		t.Fatalf("run snapshot after failed refresh = %+v, want retained run-1", got)
	}
	if got := m.statusBar.Error; got != refreshErr.Error() {
		t.Errorf("refresh error shown in status bar = %q, want %q", got, refreshErr)
	}

	updated, cmd = m.Update(runActivityPollMsg{gen: m.runActivityPollGen})
	m = updated.(*Model)
	if cmd == nil {
		t.Fatal("Activity poll after a refresh error = nil, want continued polling without panic")
	}
	batch, ok := cmd().(tea.BatchMsg)
	if !ok {
		t.Fatalf("Activity poll cmd() = %T, want tea.BatchMsg", cmd())
	}
	if len(batch) != 5 {
		t.Fatalf("Activity poll batch length = %d, want 5 including the run summary and next timer", len(batch))
	}
}

func TestUpdate_RunLoadedMsg_DropsSupersededResponse(t *testing.T) {
	m := readyModel(t, &fakeClient{board: &client.BoardPayload{}})
	m.runSnapshotReqGen = 2

	updated, _ := m.Update(runLoadedMsg{gen: 2, data: &client.RunPayload{RunID: "run-new", Status: "running"}})
	m = updated.(*Model)
	updated, cmd := m.Update(runLoadedMsg{gen: 1, data: &client.RunPayload{RunID: "run-old", Status: "finished"}})
	m = updated.(*Model)
	if cmd != nil {
		t.Error("superseded run response should not update loading state")
	}
	if got := m.run.GetData(); got == nil || got.RunID != "run-new" {
		t.Errorf("run snapshot after stale response = %+v, want run-new", got)
	}
}

// TestUpdate_RunLoadedMsg_StaleResponseClearsLoadingState is a regression
// test for the bug where a superseded runLoadedMsg was dropped without ever
// calling finishLoad, leaving m.loading stuck true (frozen on "Loading
// Run...") if no other in-flight response ever arrives to clear it.
func TestUpdate_RunLoadedMsg_StaleResponseClearsLoadingState(t *testing.T) {
	m := readyModel(t, &fakeClient{board: &client.BoardPayload{}})
	m.screen = screenRun
	m.loading = true
	m.runSnapshotReqGen = 2

	updated, cmd := m.Update(runLoadedMsg{gen: 1, data: &client.RunPayload{RunID: "run-old", Status: "finished"}})
	m = updated.(*Model)
	if cmd != nil {
		t.Errorf("stale run response cmd = %v, want nil", cmd)
	}
	if m.loading {
		t.Error("stale runLoadedMsg left m.loading = true, want it cleared")
	}
}

func TestUpdate_RunActivityPollRefreshesLiveDataAndStopsInAuditMode(t *testing.T) {
	fc := &fakeClient{
		board:     &client.BoardPayload{},
		run:       &client.RunPayload{RunID: "run-1", Status: "running"},
		runEvents: &client.RunEventsPayload{Events: []client.ExecutorEvent{{TicketID: "TICK-1", Progress: client.ExecutorProgress{Activity: "planning"}}}},
	}
	m := readyModel(t, fc)
	updated, cmd := m.Update(key("5"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)

	callsBeforePoll := fc.runEventsCalls
	updated, cmd = m.Update(runActivityPollMsg{gen: m.runActivityPollGen})
	m = updated.(*Model)
	if cmd == nil {
		t.Fatal("live Activity poll should refresh run data")
	}
	msg := cmd()
	batch, ok := msg.(tea.BatchMsg)
	if !ok {
		t.Fatalf("Activity poll cmd() = %T, want tea.BatchMsg", msg)
	}
	for _, c := range batch[:3] { // The fourth command schedules the next timer.
		updated, _ = m.Update(c())
		m = updated.(*Model)
	}
	if fc.runEventsCalls != callsBeforePoll+1 {
		t.Errorf("GetRunEvents calls after poll = %d, want %d", fc.runEventsCalls, callsBeforePoll+1)
	}

	updated, _ = m.Update(key("a"))
	m = updated.(*Model)
	if m.runActivityPolling {
		t.Error("entering Raw Audit Log should stop the Activity poller")
	}
	updated, cmd = m.Update(runActivityPollMsg{gen: m.runActivityPollGen - 1})
	m = updated.(*Model)
	if cmd != nil {
		t.Error("a stale Activity timer must not reload while Raw Audit Log is active")
	}
}

// TestUpdate_RunActivityPollMsg_SelfHealsWhenScreenChangesWithoutStop is a
// regression test: if a screen/audit-mode transition ever leaves
// m.screen/audit-mode diverged from Run/Activity without going through
// stopRunActivityPolling first, the runActivityPollMsg handler must clear
// runActivityPolling itself instead of silently dropping its reschedule
// chain while leaving the flag stuck true — otherwise ensureRunActivityPolling's
// guard permanently refuses to ever restart polling.
func TestUpdate_RunActivityPollMsg_SelfHealsWhenScreenChangesWithoutStop(t *testing.T) {
	fc := &fakeClient{
		board:     &client.BoardPayload{},
		run:       &client.RunPayload{RunID: "run-1", Status: "running"},
		runEvents: &client.RunEventsPayload{Events: []client.ExecutorEvent{{TicketID: "TICK-1", Progress: client.ExecutorProgress{Activity: "planning"}}}},
	}
	m := readyModel(t, fc)
	updated, cmd := m.Update(key("5"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)
	if !m.runActivityPolling {
		t.Fatal("test setup: expected live Activity polling to be armed after entering the Run screen")
	}
	gen := m.runActivityPollGen

	// Simulate a transition that diverges m.screen from Run without going
	// through switchScreen/stopRunActivityPolling, so runActivityPolling is
	// still true and gen still matches when the pending tick fires.
	m.screen = screenBoard

	updated, cmd = m.Update(runActivityPollMsg{gen: gen})
	m = updated.(*Model)
	if cmd != nil {
		t.Error("a poll message that declines to reschedule itself should not return a cmd")
	}
	if m.runActivityPolling {
		t.Fatal("runActivityPollMsg must self-heal runActivityPolling to false when it drops its own reschedule chain, not leave it stuck true")
	}

	// Re-enter the Run screen and drive a runEventsLoadedMsg response;
	// ensureRunActivityPolling's guard must no longer permanently refuse to
	// restart the poller.
	updated, cmd = m.Update(key("5"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)
	if !m.runActivityPolling {
		t.Error("live Activity polling should re-arm after switchScreen(screenRun) + runEventsLoadedMsg, proving the poller is not permanently wedged")
	}
}

func TestUpdate_RunActivityPoll_PreservesLiveRunScroll(t *testing.T) {
	events := make([]client.ExecutorEvent, 30)
	for i := range events {
		events[i] = client.ExecutorEvent{
			TicketID: "TICK-1",
			Progress: client.ExecutorProgress{Phase: "implementing", Activity: "planning", Executor: "claude-a"},
		}
	}
	fc := &fakeClient{
		board:     &client.BoardPayload{},
		run:       &client.RunPayload{RunID: "run-1", Status: "running"},
		runEvents: &client.RunEventsPayload{Events: events},
	}
	m := readyModel(t, fc)
	updated, cmd := m.Update(key("5"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)
	m.scrollActive(3)
	wantOffset := m.scrollOffsets[screenRun]
	if wantOffset == 0 {
		t.Fatal("test setup failed to scroll away from the top")
	}

	fc.runEvents = &client.RunEventsPayload{Events: append(events, client.ExecutorEvent{
		TicketID: "TICK-2",
		Progress: client.ExecutorProgress{Phase: "testing", Activity: "testing", Executor: "claude-b"},
	})}
	updated, cmd = m.Update(runActivityPollMsg{gen: m.runActivityPollGen})
	m = updated.(*Model)
	batch, ok := cmd().(tea.BatchMsg)
	if !ok {
		t.Fatalf("Activity poll cmd() = %T, want tea.BatchMsg", cmd())
	}
	for _, c := range batch[:2] { // Skip the timer reschedule command.
		updated, _ = m.Update(c())
		m = updated.(*Model)
	}
	if got := m.scrollOffsets[screenRun]; got != wantOffset {
		t.Errorf("scroll offset after Activity poll = %d, want %d", got, wantOffset)
	}
}

func TestUpdate_RunKeysScrollEvenWhenHistoryIsPopulated(t *testing.T) {
	events := make([]client.ExecutorEvent, 40)
	for i := range events {
		events[i] = client.ExecutorEvent{
			TicketID: "TICK-1",
			Progress: client.ExecutorProgress{Phase: "implementing", Activity: "planning", Executor: "claude-a"},
		}
	}
	fc := &fakeClient{
		board:     &client.BoardPayload{},
		run:       &client.RunPayload{RunID: "run-live", Status: "running"},
		runEvents: &client.RunEventsPayload{Events: events},
	}
	m := readyModel(t, fc)
	updated, cmd := m.Update(key("5"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)
	m.run.SetHistory(&client.RunHistoryPayload{Runs: []client.RunSummaryPayload{{RunID: "run-live"}, {RunID: "run-old"}}})

	selected := m.run.SelectedRun().RunID
	updated, cmd = m.Update(key("j"))
	m = updated.(*Model)
	if cmd != nil {
		t.Error("j on the live Run screen should only scroll")
	}
	if got := m.run.SelectedRun().RunID; got != selected {
		t.Errorf("j changed historical selection to %q, want %q", got, selected)
	}
	if got := m.scrollOffsets[screenRun]; got == 0 {
		t.Error("j did not scroll the live Run screen")
	}
}

// TestUpdate_RunHistorySelectionScrollsRowIntoView is a regression test for
// a bug where moving the Run History selection with j/k jumped the viewport
// to the "Selected Run:" detail line below the table instead of the table
// row itself — scrollToSelectedRunHistory used to find the target line by
// searching rendered text for the raw run id, but the table's STARTED
// column stopped rendering that raw id (it shows a formatted timestamp),
// leaving the detail line as the only remaining match. The fix computes the
// row's line directly instead of searching for it.
func TestUpdate_RunHistorySelectionScrollsRowIntoView(t *testing.T) {
	const runCount = 30
	runs := make([]client.RunSummaryPayload, runCount)
	for i := range runs {
		runs[i] = client.RunSummaryPayload{RunID: fmt.Sprintf("run-%02d", i), Reason: "stopped"}
	}
	fc := &fakeClient{board: &client.BoardPayload{}, runHistory: &client.RunHistoryPayload{Runs: runs}}
	m := readyModel(t, fc)
	updated, cmd := m.Update(key("6"))
	m = updated.(*Model)
	if cmd == nil {
		t.Fatal("switching to the History screen: cmd = nil, want a run-history load")
	}
	updated, _ = m.Update(cmd())
	m = updated.(*Model)

	height := m.bodyHeight()
	if height <= 0 {
		t.Fatal("test terminal too short to exercise scrolling")
	}

	for step := 1; step < runCount; step++ {
		updated, _ = m.Update(key("j"))
		m = updated.(*Model)

		if got := m.run.SelectedIndex(); got != step {
			t.Fatalf("after %d 'j' presses, selected index = %d, want %d (selection should move one row at a time)", step, got, step)
		}
		line, ok := m.run.SelectedRunRenderedLine()
		if !ok {
			t.Fatalf("step %d: SelectedRunRenderedLine reported no selection", step)
		}
		offset := m.scrollOffsets[screenHistory]
		if line < offset || line > offset+height-1 {
			t.Fatalf("step %d: selected row at line %d is outside the visible window [%d, %d) — selection scrolled out of view", step, line, offset, offset+height)
		}
	}
}

func TestUpdate_RunActivityPollTagsOnlyItsOwnRunLoadAsBackground(t *testing.T) {
	events := make([]client.ExecutorEvent, 30)
	for i := range events {
		events[i] = client.ExecutorEvent{
			TicketID: "TICK-1",
			Progress: client.ExecutorProgress{Phase: "implementing", Activity: "planning"},
		}
	}
	fc := &fakeClient{
		board:     &client.BoardPayload{},
		run:       &client.RunPayload{RunID: "run-1", Status: "running"},
		runEvents: &client.RunEventsPayload{Events: events},
	}
	m := readyModel(t, fc)
	updated, cmd := m.Update(key("5"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)
	m.scrollActive(3)
	if m.scrollOffsets[screenRun] == 0 {
		t.Fatal("test setup failed to scroll away from the top")
	}

	updated, cmd = m.Update(runActivityPollMsg{gen: m.runActivityPollGen})
	m = updated.(*Model)
	if cmd == nil {
		t.Fatal("Activity poll should schedule a refresh")
	}

	// An ordinary load can complete while the poll's request is in flight. It
	// must retain normal foreground behavior instead of consuming the poll's
	// preserve-scroll state.
	updated, _ = m.Update(runLoadedMsg{data: &client.RunPayload{RunID: "run-1", Status: "running"}})
	m = updated.(*Model)
	if got := m.scrollOffsets[screenRun]; got != 0 {
		t.Errorf("foreground run load scroll offset = %d, want 0", got)
	}

	m.scrollActive(3)
	wantOffset := m.scrollOffsets[screenRun]
	updated, _ = m.Update(runLoadedMsg{
		data:           &client.RunPayload{RunID: "run-1", Status: "running"},
		autoRefreshing: true,
	})
	m = updated.(*Model)
	if got := m.scrollOffsets[screenRun]; got != wantOffset {
		t.Errorf("background run load scroll offset = %d, want %d", got, wantOffset)
	}
}

func TestUpdate_RunActivityPoll_KeepsUserPinnedAtTopWhenNotScrolled(t *testing.T) {
	events := make([]client.ExecutorEvent, 30)
	for i := range events {
		events[i] = client.ExecutorEvent{TicketID: "TICK-1", Progress: client.ExecutorProgress{Activity: "planning"}}
	}
	fc := &fakeClient{
		board:     &client.BoardPayload{},
		run:       &client.RunPayload{RunID: "run-1", Status: "running"},
		runEvents: &client.RunEventsPayload{Events: events},
	}
	m := readyModel(t, fc)
	updated, cmd := m.Update(key("5"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)
	if got := m.scrollOffsets[screenRun]; got != 0 {
		t.Fatalf("test setup scroll offset = %d, want 0", got)
	}
	fc.runEvents = &client.RunEventsPayload{Events: append(events, client.ExecutorEvent{TicketID: "TICK-2", Progress: client.ExecutorProgress{Activity: "testing"}})}
	updated, cmd = m.Update(runActivityPollMsg{gen: m.runActivityPollGen})
	m = updated.(*Model)
	batch, ok := cmd().(tea.BatchMsg)
	if !ok {
		t.Fatal("Activity poll should return a command batch")
	}
	for _, c := range batch[:3] { // Skip the timer reschedule command.
		updated, _ = m.Update(c())
		m = updated.(*Model)
	}
	if got := m.scrollOffsets[screenRun]; got != 0 {
		t.Errorf("scroll offset after Activity poll = %d, want 0", got)
	}
}

func TestUpdate_RunCopyKeyCopiesAllLoadedActivityWithoutViewportTruncation(t *testing.T) {
	fc := &fakeClient{board: &client.BoardPayload{}}
	m := readyModel(t, fc)
	m.screen = screenRun
	m.run.SetActivityEvents("", &client.RunEventsPayload{Events: []client.ExecutorEvent{
		{Ts: "2026-08-01T04:50:36Z", TicketID: "TICK-1", Progress: client.ExecutorProgress{Phase: "implementing", Activity: "planning", Executor: "claude-a", Model: "model-a"}},
		{Ts: "2026-08-01T04:50:37Z", TicketID: "TICK-2", Progress: client.ExecutorProgress{Phase: "testing", Activity: "testing", Executor: "codex-a", Model: "model-b"}},
	}})

	updated, cmd := m.Update(key("c"))
	m = updated.(*Model)
	if cmd != nil {
		t.Error("copy key should not start a network request")
	}
	const prefix = "\x1b]52;c;"
	if !strings.HasPrefix(m.clipboardEscape, prefix) {
		t.Fatalf("clipboard sequence = %q, want OSC 52 prefix", m.clipboardEscape)
	}
	encoded := strings.TrimSuffix(strings.TrimPrefix(m.clipboardEscape, prefix), "\a")
	decoded, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		t.Fatalf("clipboard payload is not base64: %v", err)
	}
	for _, want := range []string{"TICK-1", "TICK-2", "planning", "testing"} {
		if !strings.Contains(string(decoded), want) {
			t.Errorf("copied Activity missing %q:\n%s", want, decoded)
		}
	}
	if got := m.View(); !strings.HasPrefix(got, prefix) {
		t.Error("first render after copy should emit the terminal clipboard sequence")
	}
	if got := m.View(); strings.HasPrefix(got, prefix) {
		t.Error("clipboard sequence should be emitted only once")
	}
}

func TestUpdate_RunCopyRangeKeysCopyAcrossScrolledPages(t *testing.T) {
	fc := &fakeClient{board: &client.BoardPayload{}}
	m := readyModel(t, fc)
	m.screen = screenRun
	m.height = 6
	m.run.SetData(&client.RunPayload{RunID: "run-copy", Status: "running"})
	m.run.SetActivityEvents("", &client.RunEventsPayload{Events: []client.ExecutorEvent{
		{Ts: "2026-08-01T04:50:36Z", TicketID: "TICK-FIRST", Progress: client.ExecutorProgress{Phase: "implementing", Activity: "planning", Executor: "claude-a"}},
		{Ts: "2026-08-01T04:50:37Z", TicketID: "TICK-SECOND", Progress: client.ExecutorProgress{Phase: "implementing", Activity: "searching", Executor: "claude-a"}},
		{Ts: "2026-08-01T04:50:38Z", TicketID: "TICK-LAST", Progress: client.ExecutorProgress{Phase: "implementing", Activity: "writing_file", Executor: "claude-a"}},
	}})

	bodyLines := strings.Split(m.currentBody(), "\n")
	first, last := -1, -1
	for i, line := range bodyLines {
		if strings.Contains(line, "TICK-FIRST") {
			first = i
		}
		if strings.Contains(line, "TICK-LAST") {
			last = i
		}
	}
	if first < 0 || last < first {
		t.Fatalf("test Run body did not contain expected activity range: %q", m.currentBody())
	}
	m.scrollOffsets[screenRun] = first
	updated, _ := m.Update(key("v"))
	m = updated.(*Model)
	m.scrollOffsets[screenRun] = last
	updated, _ = m.Update(key("y"))
	m = updated.(*Model)
	const prefix = "\x1b]52;c;"
	encoded := strings.TrimSuffix(strings.TrimPrefix(m.clipboardEscape, prefix), "\a")
	decoded, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		t.Fatalf("range clipboard payload is not base64: %v", err)
	}
	for _, want := range []string{"TICK-FIRST", "TICK-SECOND", "TICK-LAST"} {
		if !strings.Contains(string(decoded), want) {
			t.Errorf("range copy missing %q:\n%s", want, decoded)
		}
	}
}

func TestUpdate_RunAuditKeyGatesRawLogsAndPages(t *testing.T) {
	fc := &fakeClient{
		board: &client.BoardPayload{},
		run:   &client.RunPayload{RunID: "run-1", Status: "running"},
		runLogs: &client.RunLogsPayload{
			RunID: "run-1", Events: []client.LogEvent{{Message: "raw executor protocol line"}}, TotalCount: 2, Offset: 0, Limit: 1,
		},
		runLogStreamBodies: []string{"id: 1\nevent: log\ndata: {\"id\":\"1\",\"message\":\"live raw line\"}\n\n"},
	}
	m := readyModel(t, fc)
	updated, cmd := m.Update(key("5"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)
	if strings.Contains(m.View(), "raw executor protocol line") {
		t.Fatal("default Activity view exposed raw audit output")
	}

	updated, cmd = m.Update(key("a"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)
	if !m.run.IsAuditMode() || fc.runLogsCalls != 1 || fc.runLogStreamCalls != 1 {
		t.Fatalf("audit toggle mode/logs/stream = %v/%d/%d, want true/1/1", m.run.IsAuditMode(), fc.runLogsCalls, fc.runLogStreamCalls)
	}
	if got := m.View(); !strings.Contains(got, "Raw Audit Log") || !strings.Contains(got, "raw executor protocol line") {
		t.Fatalf("audit view missing paginated raw output:\n%s", got)
	}
	if got := m.View(); !strings.Contains(got, "entries 1-1/2") {
		t.Fatalf("audit view footer missing pinned page position:\n%s", got)
	}
	updated, cmd = m.Update(key("n"))
	m = updated.(*Model)
	if cmd == nil {
		t.Fatal("n in Raw Audit Log should load the next page")
	}
	updated, _ = m.Update(cmd())
	m = updated.(*Model)
	if fc.runLogsCalls != 2 || fc.lastRunLogsOffset != 1 {
		t.Fatalf("audit page calls/last offset = %d/%d, want 2/1", fc.runLogsCalls, fc.lastRunLogsOffset)
	}

	updated, cmd = m.Update(key("a"))
	m = updated.(*Model)
	if cmd == nil || m.run.IsAuditMode() || m.runStream != nil || !m.runActivityPolling {
		t.Fatal("second audit key must return to Activity, stop the raw stream, and restart Activity refresh")
	}
}

func TestUpdate_RunHistorySelectionOpensHistoricalActivityOnEnter(t *testing.T) {
	fc := &fakeClient{
		board:     &client.BoardPayload{},
		run:       &client.RunPayload{RunID: "run-live", Status: "running"},
		runEvents: &client.RunEventsPayload{RunID: "run-old", Events: []client.ExecutorEvent{{TicketID: "TICK-OLD", Progress: client.ExecutorProgress{Activity: "completed"}}}},
	}
	m := readyModel(t, fc)
	updated, cmd := m.Update(key("6"))
	m = updated.(*Model)
	updated, _ = m.Update(cmd())
	m = updated.(*Model)
	m.run.SetHistory(&client.RunHistoryPayload{Runs: []client.RunSummaryPayload{{RunID: "run-live"}, {RunID: "run-old"}}})

	updated, cmd = m.Update(key("down"))
	m = updated.(*Model)
	if cmd != nil {
		t.Fatal("historical selection should remain local until Enter opens it")
	}
	updated, cmd = m.Update(key("enter"))
	m = updated.(*Model)
	if cmd == nil {
		t.Fatal("Enter on a historical selection should request its structured event payload")
	}
	updated, _ = m.Update(cmd())
	m = updated.(*Model)
	if fc.lastRunEventsID != "run-old" || m.run.ActivityRunID() != "run-old" || !m.run.IsHistoryDetail() {
		t.Fatalf("historical events id/activity id = %q/%q, want run-old", fc.lastRunEventsID, m.run.ActivityRunID())
	}
	updated, cmd = m.Update(key("esc"))
	m = updated.(*Model)
	if cmd != nil || m.run.IsHistoryDetail() {
		t.Fatal("Esc should close the historical detail and return to the history list")
	}
}

// TestUpdate_RunLogStream_ReconnectsOnDrop drives the Run screen's live log
// stream through two full read cycles: the first stream yields one event and
// then EOF, which client.ReconnectingStream must transparently recover from
// by reopening the stream (via OpenRunLogStream again) rather than surfacing
// the drop as a fatal stream error.
func TestUpdate_RunLogStream_ReconnectsOnDrop(t *testing.T) {
	fc := &fakeClient{
		board: &client.BoardPayload{},
		run:   &client.RunPayload{RunID: "run-1", Status: "running"},
		runLogStreamBodies: []string{
			"id: 1\nevent: log\ndata: {\"id\":\"1\",\"message\":\"first\"}\n\n",
			"id: 2\nevent: log\ndata: {\"id\":\"2\",\"message\":\"second\"}\n\n",
		},
	}
	m := readyModel(t, fc)

	updated, cmd := m.Update(key("5"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)
	updated, cmd = m.Update(key("a"))
	m = updated.(*Model)
	m, logCmd := runBatchCmds(t, m, cmd)

	if got := m.run.LogLines(); len(got) != 1 || got[0] != "first" {
		t.Fatalf("log lines after first read = %v, want [first]", got)
	}
	if logCmd == nil {
		t.Fatal("expected a self-reissued run-log read command after the first event")
	}

	// Reading again drives the stream past its first body's EOF, forcing
	// client.ReconnectingStream to call OpenRunLogStream a second time.
	msg := logCmd()
	lm, ok := msg.(runLogMsg)
	if !ok {
		t.Fatalf("second read = %T, want runLogMsg", msg)
	}
	if lm.err != nil {
		t.Fatalf("expected the drop to be recovered via reconnect, got error: %v", lm.err)
	}
	updated, _ = m.Update(lm)
	m = updated.(*Model)

	if fc.runLogStreamCalls != 2 {
		t.Errorf("OpenRunLogStream calls = %d, want 2 (initial connect + one reconnect)", fc.runLogStreamCalls)
	}
	if got := m.run.LogLines(); len(got) != 2 || got[1] != "second" {
		t.Errorf("log lines after reconnect = %v, want [first second]", got)
	}
	if m.run.GetData() == nil {
		t.Error("expected run snapshot data to remain set across the reconnect")
	}
}

// TestUpdate_RunLogMsg_StaleGenerationIsDropped documents why runLogMsg
// carries a generation counter: leaving the Run screen stops the old stream,
// but a read that was already in flight can still deliver its message after
// that. Update must recognize it belongs to an abandoned stream and drop it
// rather than corrupting whichever screen is now active.
func TestUpdate_RunLogMsg_StaleGenerationIsDropped(t *testing.T) {
	fc := &fakeClient{
		board: &client.BoardPayload{},
		run:   &client.RunPayload{RunID: "run-1"},
	}
	m := readyModel(t, fc)

	updated, _ := m.Update(key("5"))
	m = updated.(*Model)
	staleGen := m.runStreamGen

	updated, _ = m.Update(key("1"))
	m = updated.(*Model)
	if m.screen != screenBoard {
		t.Fatalf("screen after pressing 1 = %v, want screenBoard", m.screen)
	}

	updated, cmd := m.Update(runLogMsg{gen: staleGen, ev: client.LogEvent{Message: "late"}})
	m = updated.(*Model)
	if cmd != nil {
		t.Error("a stale runLogMsg should not reissue a read command")
	}
	for _, line := range m.run.LogLines() {
		if line == "late" {
			t.Error("a stale runLogMsg should not be applied to the run screen")
		}
	}
}

// TestUpdate_PressH_OnRunScreen_FetchesOlderHistory drives the Run screen's
// "H" key: once the live tail has at least one event establishing a
// boundary, pressing H should call GetRunLogPage for the page immediately
// preceding that boundary and splice the result into RunModel's history.
func TestUpdate_PressH_OnRunScreen_FetchesOlderHistory(t *testing.T) {
	next := 400
	fc := &fakeClient{
		board: &client.BoardPayload{},
		run:   &client.RunPayload{RunID: "run-1000", Status: "running"},
		logPageResult: &client.LogPagePayload{
			RunID:      "run-1000",
			Offset:     400,
			Limit:      200,
			TotalCount: 1000,
			NextOffset: &next,
			Events: []client.LogEvent{
				{ID: "401", Message: "older-1"},
				{ID: "402", Message: "older-2"},
			},
		},
	}
	m := readyModel(t, fc)

	updated, cmd := m.Update(key("5"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)

	// Enter Audit mode so raw logs are accepted
	updated, cmd = m.Update(key("a"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)

	// Establish the live-tail boundary at line 600 (as if 1000 events were
	// streamed and only the last 200, 601..800 shown here abbreviated, remain
	// after the tail cap).
	updated, _ = m.Update(runLogMsg{gen: m.runStreamGen, ev: client.LogEvent{ID: "601", Message: "tail-start"}})
	m = updated.(*Model)

	updated, histCmd := m.Update(key("H"))
	m = updated.(*Model)
	if histCmd == nil {
		t.Fatal("pressing H should issue a history-fetch command once the tail boundary is known")
	}

	msg := histCmd()
	updated, _ = m.Update(msg)
	m = updated.(*Model)

	if len(fc.logPageCalls) != 1 {
		t.Fatalf("GetRunLogPage calls = %d, want 1", len(fc.logPageCalls))
	}
	if got := fc.logPageCalls[0]; got.offset != 400 || got.limit != 200 {
		t.Errorf("GetRunLogPage(offset, limit) = (%d, %d), want (400, 200)", got.offset, got.limit)
	}
	if got := m.run.HistoryLines(); len(got) != 2 || got[0] != "older-1" || got[1] != "older-2" {
		t.Errorf("history lines = %v, want [older-1 older-2]", got)
	}
}

// TestUpdate_PressH_OnRunScreen_NoOpWithoutTailBoundary documents that
// pressing H before any live-tail event has arrived does nothing (there is
// nothing to page backward from yet) rather than issuing a malformed
// request.
func TestUpdate_PressH_OnRunScreen_NoOpWithoutTailBoundary(t *testing.T) {
	fc := &fakeClient{
		board: &client.BoardPayload{},
		run:   &client.RunPayload{RunID: "run-1"},
	}
	m := readyModel(t, fc)
	updated, cmd := m.Update(key("5"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)

	updated, cmd = m.Update(key("a"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)

	updated, histCmd := m.Update(key("H"))
	m = updated.(*Model)
	if histCmd != nil {
		t.Error("pressing H with no live-tail boundary yet should be a no-op")
	}
	if len(fc.logPageCalls) != 0 {
		t.Errorf("GetRunLogPage calls = %d, want 0", len(fc.logPageCalls))
	}
}

// TestUpdate_PressH_SurfacesHistoryFetchError verifies a failed history page
// fetch is recorded as a visible error rather than silently dropped or
// mistaken for "no more history".
func TestUpdate_PressH_SurfacesHistoryFetchError(t *testing.T) {
	fc := &fakeClient{
		board:      &client.BoardPayload{},
		run:        &client.RunPayload{RunID: "run-1", Status: "running"},
		logPageErr: errors.New("connection refused"),
	}
	m := readyModel(t, fc)
	updated, cmd := m.Update(key("5"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)

	updated, cmd = m.Update(key("a"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)

	updated, _ = m.Update(runLogMsg{gen: m.runStreamGen, ev: client.LogEvent{ID: "5", Message: "tail"}})
	m = updated.(*Model)

	updated, histCmd := m.Update(key("H"))
	m = updated.(*Model)
	if histCmd == nil {
		t.Fatal("expected a history-fetch command")
	}
	updated, _ = m.Update(histCmd())
	m = updated.(*Model)

	if got := m.run.HistoryError(); got == "" {
		t.Error("expected HistoryError to be set after a failed fetch")
	}
}

// TestUpdate_PressHome_OnRunScreen_AlsoTriggersHistoryFetch documents that
// "home" both scrolls to the top and kicks off loading older Activity, so a
// user reaching for the natural "show me the start" gesture gets it without
// needing to separately discover the H key.
func TestUpdate_PressHome_OnRunScreen_AlsoTriggersHistoryFetch(t *testing.T) {
	fc := &fakeClient{
		board: &client.BoardPayload{},
		run:   &client.RunPayload{RunID: "run-1", Status: "running"},
		logPageResult: &client.LogPagePayload{
			RunID: "run-1", Offset: 0, Limit: 5,
			Events: []client.LogEvent{{ID: "1", Message: "start"}},
		},
	}
	m := readyModel(t, fc)
	updated, cmd := m.Update(key("5"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)

	updated, cmd = m.Update(key("a"))
	m = updated.(*Model)
	m, _ = runBatchCmds(t, m, cmd)

	updated, _ = m.Update(runLogMsg{gen: m.runStreamGen, ev: client.LogEvent{ID: "5", Message: "tail"}})
	m = updated.(*Model)

	updated, histCmd := m.Update(key("home"))
	m = updated.(*Model)
	if histCmd == nil {
		t.Fatal("pressing home on the Run screen should also issue a history-fetch command")
	}
}

func TestUpdate_Esc_ReturnsToPreviousScreen(t *testing.T) {
	fc := &fakeClient{
		board:  &client.BoardPayload{},
		ticket: &client.TicketDetail{ID: "TICK-1"},
	}
	m := readyModel(t, fc)

	// No history yet: esc is a no-op.
	updated, cmd := m.Update(key("esc"))
	m = updated.(*Model)
	if cmd != nil {
		t.Errorf("esc with no history: cmd = %v, want nil", cmd)
	}
	if m.screen != screenBoard {
		t.Errorf("esc with no history changed screen to %v, want screenBoard", m.screen)
	}

	updated, _ = m.Update(key("2"))
	m = updated.(*Model)
	if m.screen != screenTicket {
		t.Fatalf("screen after pressing 2 = %v, want screenTicket", m.screen)
	}

	updated, cmd = m.Update(key("esc"))
	m = updated.(*Model)
	if m.screen != screenBoard {
		t.Errorf("screen after esc = %v, want screenBoard (previous screen)", m.screen)
	}
	if cmd == nil {
		t.Error("esc back to a previous screen should trigger a reload command")
	}
}

// --- Pool executor reorder ---

// settingsReadyModel switches a ready model to the Settings screen and
// seeds it with pools directly (bypassing the load round-trip, which is
// already covered by TestUpdate_ScreenSwitch_ToSettings_CallsGetSettings)
// so reorder-key tests can focus on the key-handling behavior itself.
func settingsReadyModel(t *testing.T, fc *fakeClient, pools []client.Pool) *Model {
	t.Helper()
	m := readyModel(t, fc)
	updated, _ := m.Update(key("7"))
	m = updated.(*Model)
	m.settings.SetPools(pools)
	return m
}

func TestUpdate_SettingsPoolSelection_UpDownMovesFocusedPool(t *testing.T) {
	fc := &fakeClient{board: &client.BoardPayload{}}
	m := settingsReadyModel(t, fc, []client.Pool{
		{Name: "default", Executors: []string{"claude-1"}},
		{Name: "local", Executors: []string{"ollama-a"}},
	})

	if p, ok := m.settings.SelectedPool(); !ok || p.Name != "default" {
		t.Fatalf("initial selection = %+v (ok=%v), want default", p, ok)
	}

	updated, _ := m.Update(key("down"))
	m = updated.(*Model)
	if p, ok := m.settings.SelectedPool(); !ok || p.Name != "local" {
		t.Fatalf("selection after down = %+v (ok=%v), want local", p, ok)
	}

	updated, _ = m.Update(key("j"))
	m = updated.(*Model)
	if p, ok := m.settings.SelectedPool(); !ok || p.Name != "local" {
		t.Fatalf("selection after j at the last pool = %+v (ok=%v), want clamped at local", p, ok)
	}
}

func TestUpdate_SettingsPoolReorder_PWithNoPoolsShowsInfo(t *testing.T) {
	fc := &fakeClient{board: &client.BoardPayload{}}
	m := settingsReadyModel(t, fc, nil)

	updated, cmd := m.Update(key("p"))
	m = updated.(*Model)
	if cmd != nil {
		t.Error("expected no command from 'p' with no pools configured")
	}
	if m.settings.IsEditingPool() {
		t.Error("expected IsEditingPool to stay false with no pools configured")
	}
}

func TestUpdate_SettingsPoolReorder_FullFlowSavesAndCommits(t *testing.T) {
	fc := &fakeClient{
		board: &client.BoardPayload{},
		updatePoolExecutorsResult: &client.Pool{
			Name:      "default",
			Strategy:  "least-loaded",
			Executors: []string{"claude-2", "claude-1"},
		},
	}
	m := settingsReadyModel(t, fc, []client.Pool{
		{Name: "default", Strategy: "least-loaded", Executors: []string{"claude-1", "claude-2"}},
	})

	// p: begin reordering the (only, already-focused) pool.
	updated, _ := m.Update(key("p"))
	m = updated.(*Model)
	if !m.settings.IsEditingPool() {
		t.Fatal("expected IsEditingPool to be true after 'p'")
	}

	// down: move the executor cursor onto claude-2.
	updated, _ = m.Update(key("down"))
	m = updated.(*Model)

	// K: move claude-2 above claude-1.
	updated, _ = m.Update(key("K"))
	m = updated.(*Model)
	if got := m.settings.PendingOrder(); len(got) != 2 || got[0] != "claude-2" || got[1] != "claude-1" {
		t.Fatalf("pending order after K = %v, want [claude-2 claude-1]", got)
	}

	// enter: save.
	updated, cmd := m.Update(key("enter"))
	m = updated.(*Model)
	if cmd == nil {
		t.Fatal("expected 'enter' while editing to return a save command")
	}
	msg := cmd()
	saved, ok := msg.(poolSavedMsg)
	if !ok {
		t.Fatalf("save command result = %T, want poolSavedMsg", msg)
	}
	if fc.updatePoolExecutorsCalls != 1 {
		t.Errorf("UpdatePoolExecutors calls = %d, want 1", fc.updatePoolExecutorsCalls)
	}
	if fc.lastUpdatePoolExecutorsName != "default" {
		t.Errorf("UpdatePoolExecutors pool name = %q, want default", fc.lastUpdatePoolExecutorsName)
	}
	want := []string{"claude-2", "claude-1"}
	if len(fc.lastUpdatePoolExecutorsList) != 2 || fc.lastUpdatePoolExecutorsList[0] != want[0] {
		t.Errorf("UpdatePoolExecutors executors = %v, want %v", fc.lastUpdatePoolExecutorsList, want)
	}

	updated, _ = m.Update(saved)
	m = updated.(*Model)
	if m.settings.IsEditingPool() {
		t.Error("expected the reorder to end once the save result is applied")
	}
	pool, ok := m.settings.SelectedPool()
	if !ok || len(pool.Executors) != 2 || pool.Executors[0] != "claude-2" {
		t.Fatalf("committed pool = %+v (ok=%v), want executors [claude-2 claude-1]", pool, ok)
	}
}

func TestUpdate_SettingsPoolReorder_EscCancelsWithoutSaving(t *testing.T) {
	fc := &fakeClient{board: &client.BoardPayload{}}
	m := settingsReadyModel(t, fc, []client.Pool{
		{Name: "default", Executors: []string{"claude-1", "claude-2"}},
	})

	updated, _ := m.Update(key("p"))
	m = updated.(*Model)
	updated, _ = m.Update(key("J"))
	m = updated.(*Model)

	updated, cmd := m.Update(key("esc"))
	m = updated.(*Model)
	if cmd != nil {
		t.Error("expected canceling a pool reorder with esc not to trigger a screen switch/reload")
	}
	if m.settings.IsEditingPool() {
		t.Error("expected esc to end the reorder")
	}
	if m.screen != screenSettings {
		t.Errorf("expected esc to stay on the Settings screen while canceling a reorder, got %v", m.screen)
	}
	pool, ok := m.settings.SelectedPool()
	if !ok || pool.Executors[0] != "claude-1" {
		t.Fatalf("expected esc to discard the in-progress reorder, got %+v", pool)
	}
	if fc.updatePoolExecutorsCalls != 0 {
		t.Errorf("UpdatePoolExecutors calls = %d, want 0 (esc must not save)", fc.updatePoolExecutorsCalls)
	}
}

func TestUpdate_SettingsPoolReorder_SaveErrorSetsStatusBarAndKeepsEditing(t *testing.T) {
	fc := &fakeClient{
		board:                  &client.BoardPayload{},
		updatePoolExecutorsErr: errors.New("no such pool"),
	}
	m := settingsReadyModel(t, fc, []client.Pool{
		{Name: "default", Executors: []string{"claude-1", "claude-2"}},
	})

	updated, _ := m.Update(key("p"))
	m = updated.(*Model)
	updated, cmd := m.Update(key("enter"))
	m = updated.(*Model)
	msg := cmd()

	updated, _ = m.Update(msg)
	m = updated.(*Model)
	if m.statusBar.Error != "no such pool" {
		t.Errorf("statusBar.Error = %q, want %q", m.statusBar.Error, "no such pool")
	}
	if !m.settings.IsEditingPool() {
		t.Error("expected a failed save to leave the reorder in progress (nothing was committed)")
	}
	pool, ok := m.settings.SelectedPool()
	if !ok || pool.Executors[0] != "claude-1" {
		t.Fatalf("expected the pool's saved order to be untouched by a failed save, got %+v", pool)
	}
}

func TestUpdate_RefreshKey_ReloadsActiveScreenOnly(t *testing.T) {
	fc := &fakeClient{board: &client.BoardPayload{}}
	m := readyModel(t, fc)
	callsBeforeRefresh := fc.boardCalls

	updated, cmd := m.Update(key("r"))
	m = updated.(*Model)
	if m.screen != screenBoard {
		t.Fatalf("refresh should not change the active screen, got %v", m.screen)
	}
	if cmd == nil {
		t.Fatal("Update(r) cmd = nil, want a reload command")
	}
	cmd()
	if fc.boardCalls != callsBeforeRefresh+1 {
		t.Errorf("GetBoard calls after refresh = %d, want %d", fc.boardCalls, callsBeforeRefresh+1)
	}
}

func TestUpdate_BoardMilestoneGroupingTogglePreservesSelection(t *testing.T) {
	fc := &fakeClient{board: &client.BoardPayload{Tickets: map[string][]client.Ticket{
		"open": {
			{ID: "TICK-1", Milestone: "v2", Priority: 1},
			{ID: "TICK-2", Milestone: "v1", Priority: 2},
		},
		"in_progress": {{ID: "TICK-3", Milestone: "v1", Priority: 1}},
	}}}
	m := readyModel(t, fc)
	updated, _ := m.Update(key("down"))
	m = updated.(*Model)
	if m.selectedTicketID != "TICK-2" {
		t.Fatalf("selectedTicketID before grouping = %q, want TICK-2", m.selectedTicketID)
	}

	updated, cmd := m.Update(key("m"))
	m = updated.(*Model)
	if cmd != nil {
		t.Error("milestone grouping should be a local board operation")
	}
	if got := m.board.Grouping(); got != screens.BoardGroupByMilestone {
		t.Errorf("board grouping = %v, want milestone", got)
	}
	if m.selectedTicketID != "TICK-2" || m.board.SelectedTicketID() != "TICK-2" {
		t.Errorf("selection was not preserved: app=%q board=%q", m.selectedTicketID, m.board.SelectedTicketID())
	}
	if got := m.statusBar.Info; got != "Board grouped by milestone." {
		t.Errorf("status bar info = %q, want grouping confirmation", got)
	}
}

func TestUpdate_LoadError_SetsStatusBarErrorAndKeepsRunning(t *testing.T) {
	fc := &fakeClient{
		board:     &client.BoardPayload{},
		ticket:    &client.TicketDetail{},
		ticketErr: errors.New("ticket TICK-999 not found"),
	}
	m := readyModel(t, fc)

	updated, cmd := m.Update(key("2"))
	m = updated.(*Model)
	msg := cmd()
	updated, _ = m.Update(msg)
	m = updated.(*Model)

	if m.loading {
		t.Error("expected loading to clear after an error response")
	}
	if m.statusBar.Error == "" {
		t.Error("expected the status bar to carry the load error")
	}

	// The app must keep running (no panic, no crash) and still render.
	view := m.View()
	if view == "" {
		t.Error("expected a non-empty View() after a load error")
	}
}

func TestView_ReflectsActiveScreen(t *testing.T) {
	fc := &fakeClient{
		board: &client.BoardPayload{
			Tickets: map[string][]client.Ticket{"open": {{ID: "TICK-1", Title: "Board Ticket"}}},
		},
		blocked: &client.BlockedPayload{
			Blocked: []client.BlockedTicket{{ID: "TICK-2", Title: "Blocked Ticket"}},
		},
	}
	m := readyModel(t, fc)

	boardView := m.View()
	if !strings.Contains(boardView, "TICK-1") {
		t.Errorf("Board view missing TICK-1:\n%s", boardView)
	}

	updated, cmd := m.Update(key("3"))
	m = updated.(*Model)
	updated, _ = m.Update(cmd())
	m = updated.(*Model)

	blockedView := m.View()
	if !strings.Contains(blockedView, "TICK-2") {
		t.Errorf("Blocked view missing TICK-2:\n%s", blockedView)
	}
	if strings.Contains(blockedView, "TICK-1") {
		t.Errorf("Blocked view should not still show Board content:\n%s", blockedView)
	}
}

func TestView_ShowsNeedsAttentionCountOnEveryScreen(t *testing.T) {
	fc := &fakeClient{
		board: &client.BoardPayload{Tickets: map[string][]client.Ticket{
			"needs_review": {{ID: "TICK-1", NeedsAttention: true}, {ID: "TICK-2", NeedsAttention: true}},
			"open":         {{ID: "TICK-3"}},
		}},
		ticket:   &client.TicketDetail{ID: "TICK-1"},
		blocked:  &client.BlockedPayload{},
		diff:     &client.DiffPayload{},
		run:      &client.RunPayload{},
		settings: &client.SettingsPayload{},
		pools:    &client.PoolsPayload{},
	}
	m := readyModel(t, fc)
	for _, keyName := range []string{"1", "2", "3", "4", "5", "6"} {
		updated, cmd := m.Update(key(keyName))
		m = updated.(*Model)
		if cmd != nil {
			updated, _ = m.Update(cmd())
			m = updated.(*Model)
		}
		if view := m.View(); !strings.Contains(view, "attention: 2") {
			t.Errorf("screen %s did not retain attention count:\n%s", keyName, view)
		}
	}
}

func TestView_ShowsLoadingWhileFetchInFlight(t *testing.T) {
	fc := &fakeClient{board: &client.BoardPayload{}}
	m := New(fc)
	updated, _ := m.Update(tea.WindowSizeMsg{Width: 80, Height: 24})
	m = updated.(*Model)

	m.loading = true
	if got := m.View(); !strings.Contains(got, "Loading") {
		t.Errorf("View() while loading = %q, want it to mention Loading", got)
	}
}

// --- Key bindings ---
//
// DefaultKeyBindings is the single source of truth for the documented
// global/screen-local navigation model. These tests pin down its values so
// an accidental edit to keys.go is caught, since nothing else in the
// codebase currently consumes KeyBindings to enforce the contract at
// runtime (Model.Update only wires "q"/"ctrl+c" today).

func TestDefaultKeyBindings_Global(t *testing.T) {
	kb := DefaultKeyBindings()

	want := GlobalKeys{
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
		Screen7:  "7",
		Back:     "esc",
	}
	if kb.Global != want {
		t.Errorf("Global = %+v, want %+v", kb.Global, want)
	}
}

func TestDefaultKeyBindings_ScreenKeysAreNotEmpty(t *testing.T) {
	kb := DefaultKeyBindings()

	for name, sk := range map[string]ScreenKeys{
		"Board":   kb.Board,
		"Ticket":  kb.Ticket,
		"Blocked": kb.Blocked,
	} {
		if sk.Up == "" || sk.Down == "" {
			t.Errorf("%s screen keys missing Up/Down: %+v", name, sk)
		}
	}
}

func TestDefaultKeyBindings_AllReadOnlyScreensHaveScrollKeys(t *testing.T) {
	kb := DefaultKeyBindings()
	for name, sk := range map[string]ScreenKeys{
		"Board":    kb.Board,
		"Ticket":   kb.Ticket,
		"Blocked":  kb.Blocked,
		"Diff":     kb.Diff,
		"Run":      kb.Run,
		"History":  kb.History,
		"Settings": kb.Settings,
	} {
		if sk.PageUp == "" || sk.PageDown == "" || sk.Home == "" || sk.End == "" {
			t.Errorf("%s screen keys missing scroll bindings: %+v", name, sk)
		}
	}
}
