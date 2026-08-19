package screens

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/charmbracelet/lipgloss"
	"github.com/muesli/termenv"

	"lanegate/tui/internal/client"
	"lanegate/tui/internal/ui"
)

// update regenerates golden files instead of comparing against them. Run:
//
//	go -C tui test ./internal/screens/... -run TestGolden -update
var update = flag.Bool("update", false, "update golden files in testdata/")

// narrowWidth is the terminal width golden tests render at. The ticket's own
// naming convention ("*_narrow.golden") implies a small terminal; 48 columns
// is used as the documented narrow-width convention for this package since
// no other narrow-width constant exists yet in ui/viewport.go or
// ui/statusbar.go (StatusBar.Render only guards width < 20 as unusable).
const narrowWidth = 48

var ansiPattern = regexp.MustCompile(`\x1b\[[0-9;]*m`)

// stripANSI removes lipgloss/termenv color escape codes so golden files stay
// portable across environments where the color profile detection differs
// (e.g. CI vs. an interactive terminal). Rendered content, wrapping, and
// truncation are unaffected.
func stripANSI(s string) string {
	return ansiPattern.ReplaceAllString(s, "")
}

// fixturesDir resolves the shared Python/Go fixture corpus at
// tests/fixtures/tui_contracts, relative to this source file.
func fixturesDir(t *testing.T) string {
	t.Helper()
	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("could not resolve caller for fixturesDir")
	}
	// this file: tui/internal/screens/screens_test.go
	root := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "tests", "fixtures", "tui_contracts")
	if _, err := os.Stat(root); err != nil {
		t.Fatalf("fixtures root not found at %s: %v", root, err)
	}
	return root
}

func loadFixture(t *testing.T, relPath string, out interface{}) {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(fixturesDir(t), relPath))
	if err != nil {
		t.Fatalf("read fixture %s: %v", relPath, err)
	}
	if err := json.Unmarshal(data, out); err != nil {
		t.Fatalf("decode fixture %s: %v", relPath, err)
	}
}

// compareGolden compares got against testdata/name, or (with -update)
// rewrites the golden file to match got.
func compareGolden(t *testing.T, name, got string) {
	t.Helper()
	path := filepath.Join("testdata", name)

	if *update {
		if err := os.MkdirAll("testdata", 0o755); err != nil {
			t.Fatalf("mkdir testdata: %v", err)
		}
		if err := os.WriteFile(path, []byte(got), 0o644); err != nil {
			t.Fatalf("write golden file %s: %v", path, err)
		}
		return
	}

	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read golden file %s (run with -update to create it): %v", path, err)
	}
	if got != string(want) {
		t.Errorf("output does not match golden file %s (run with -update to refresh)\n--- got ---\n%s\n--- want ---\n%s", path, got, string(want))
	}
}

// --- BoardModel ---

func TestBoardModel_SetDataGetData(t *testing.T) {
	bm := NewBoardModel()
	if bm.GetData() == nil {
		t.Fatal("expected NewBoardModel to start with non-nil data")
	}

	var payload client.BoardPayload
	loadFixture(t, "board/board_basic.json", &payload)

	bm.SetData(&payload)
	if bm.GetData() != &payload {
		t.Error("GetData did not return the payload set via SetData")
	}
	if len(bm.GetData().Tickets["open"]) != 2 {
		t.Errorf("expected 2 open tickets, got %d", len(bm.GetData().Tickets["open"]))
	}
}

func TestBoardModel_Render_Empty(t *testing.T) {
	bm := NewBoardModel()
	var payload client.BoardPayload
	loadFixture(t, "board/board_empty.json", &payload)
	bm.SetData(&payload)

	got := bm.Render(narrowWidth)
	if got != "No tickets to display." {
		t.Errorf("Render(empty) = %q, want placeholder text", got)
	}
}

func TestBoardModel_Render_NilData(t *testing.T) {
	bm := &BoardModel{}
	if got := bm.Render(narrowWidth); got != "No tickets to display." {
		t.Errorf("Render(nil data) = %q, want placeholder text", got)
	}
}

func TestBoardModel_Render_ContainsAllStatusGroups(t *testing.T) {
	bm := NewBoardModel()
	var payload client.BoardPayload
	loadFixture(t, "board/board_basic.json", &payload)
	bm.SetData(&payload)

	got := stripANSI(bm.Render(narrowWidth))
	for _, id := range []string{"TICK-150", "TICK-151", "TICK-149", "TICK-148", "TICK-147", "TICK-146"} {
		if !strings.Contains(got, id) {
			t.Errorf("Render output missing ticket %s:\n%s", id, got)
		}
	}
}

func TestBoardModel_Render_TruncatesLongTitlesAtNarrowWidth(t *testing.T) {
	bm := NewBoardModel()
	bm.SetData(&client.BoardPayload{
		Tickets: map[string][]client.Ticket{
			"open": {{
				ID:       "TICK-1",
				Title:    strings.Repeat("a very long ticket title ", 5),
				Status:   "open",
				Priority: 1,
			}},
		},
	})

	for _, line := range strings.Split(stripANSI(bm.Render(narrowWidth)), "\n") {
		// Count runes, not bytes: box-drawing separator characters (─) are
		// multi-byte in UTF-8 but render as a single terminal column.
		if n := utf8.RuneCountInString(line); n > narrowWidth+10 {
			t.Errorf("line exceeds narrow width budget (%d cols, got %d): %q", narrowWidth, n, line)
		}
	}
}

func TestBoardModel_Render_DeterministicStatusOrder(t *testing.T) {
	bm := NewBoardModel()
	var payload client.BoardPayload
	loadFixture(t, "board/board_basic.json", &payload)
	bm.SetData(&payload)

	first := bm.Render(narrowWidth)
	for i := 0; i < 5; i++ {
		if got := bm.Render(narrowWidth); got != first {
			t.Fatalf("Render output is not deterministic across repeated calls (map iteration order leaking through)")
		}
	}
}

func TestBoardModel_ToggleGrouping_GroupsAllTicketsByMilestone(t *testing.T) {
	bm := NewBoardModel()
	var payload client.BoardPayload
	loadFixture(t, "board/board_basic.json", &payload)
	bm.SetData(&payload)
	if !bm.SelectTicket("TICK-149") {
		t.Fatal("could not select fixture ticket TICK-149")
	}

	if got := bm.ToggleGrouping(); got != BoardGroupByMilestone {
		t.Fatalf("ToggleGrouping() = %v, want BoardGroupByMilestone", got)
	}
	if got := bm.GroupingLabel(); got != "milestone" {
		t.Errorf("GroupingLabel() = %q, want milestone", got)
	}
	if got := bm.SelectedTicketID(); got != "TICK-149" {
		t.Errorf("selected ticket after grouping = %q, want TICK-149", got)
	}

	rendered := stripANSI(bm.Render(narrowWidth))
	if !strings.Contains(rendered, "v1.5 (4)") || !strings.Contains(rendered, "v1.6 (2)") {
		t.Errorf("milestone groups missing from rendered board:\n%s", rendered)
	}
	if strings.Index(rendered, "v1.5 (4)") > strings.Index(rendered, "v1.6 (2)") {
		t.Errorf("milestone groups were not rendered in deterministic ascending order:\n%s", rendered)
	}
	for _, id := range []string{"TICK-150", "TICK-151", "TICK-149", "TICK-148", "TICK-147", "TICK-146"} {
		if !strings.Contains(rendered, id) {
			t.Errorf("milestone view missing ticket %s:\n%s", id, rendered)
		}
	}

	line, ok := bm.SelectedTicketRenderedLine(narrowWidth)
	lines := strings.Split(rendered, "\n")
	if !ok || line < 0 || line >= len(lines) || !strings.Contains(lines[line], "TICK-149") {
		t.Errorf("selected milestone row = line %d (ok=%t), want TICK-149:\n%s", line, ok, rendered)
	}
}

// TestBoardSelectionScrolling_MultiGroup covers the bug this ticket fixes:
// moveSelectionOrScroll used to scroll the Board viewport by one rendered
// line per one ticket of selection movement, but ticket indexes don't line
// up with rendered lines once a status header, a table header/separator
// row, and (after the first group) a blank join line sit in front of a
// group's rows. SelectedTicketRenderedLine is what update.go now consults
// instead of the index delta, so this asserts it returns the true rendered
// line for a selection landing in every group of a multi-group board,
// cross-checked against the actual Render() output rather than hand-derived
// numbers.
func TestBoardSelectionScrolling_MultiGroup(t *testing.T) {
	bm := NewBoardModel()
	var payload client.BoardPayload
	loadFixture(t, "board/board_basic.json", &payload)
	bm.SetData(&payload)

	rendered := strings.Split(stripANSI(bm.Render(narrowWidth)), "\n")
	wantIDs := []string{"TICK-150", "TICK-151", "TICK-149", "TICK-148", "TICK-147", "TICK-146"}

	for idx, wantID := range wantIDs {
		bm.selectedIndex = idx

		line, ok := bm.SelectedTicketRenderedLine(narrowWidth)
		if !ok {
			t.Fatalf("selectedIndex=%d: SelectedTicketRenderedLine returned ok=false", idx)
		}
		if line < 0 || line >= len(rendered) {
			t.Fatalf("selectedIndex=%d: line=%d out of range of %d rendered lines", idx, line, len(rendered))
		}
		if !strings.Contains(rendered[line], wantID) {
			t.Errorf("selectedIndex=%d: rendered[%d] = %q, want a row containing %s", idx, line, rendered[line], wantID)
		}
	}

	// The specific regression: moving one selection index from the last
	// ticket in "open" (index 1) to the first ticket in "in_progress"
	// (index 2) used to scroll by 1 line via the old delta-based approach,
	// but the real jump spans the blank separator plus the next group's
	// header/table-header/separator rows.
	lineAtOpen, _ := (&BoardModel{data: bm.data, selectedIndex: 1}).SelectedTicketRenderedLine(narrowWidth)
	lineAtInProgress, _ := (&BoardModel{data: bm.data, selectedIndex: 2}).SelectedTicketRenderedLine(narrowWidth)
	if delta := lineAtInProgress - lineAtOpen; delta <= 1 {
		t.Fatalf("expected moving into the next status group to span more than 1 rendered line, got delta=%d", delta)
	}
}

// --- TicketModel ---

func TestTicketModel_SetDataGetData(t *testing.T) {
	tm := NewTicketModel()
	if tm.GetData() == nil {
		t.Fatal("expected NewTicketModel to start with non-nil data")
	}

	var detail client.TicketDetail
	loadFixture(t, "ticket_detail/ticket_ready.json", &detail)

	tm.SetData(&detail)
	if tm.GetData() != &detail {
		t.Error("GetData did not return the detail set via SetData")
	}
	if tm.GetData().ID != "TICK-150" {
		t.Errorf("ID = %q, want TICK-150", tm.GetData().ID)
	}
}

func TestTicketModel_Render_NoTicketSelected(t *testing.T) {
	tm := NewTicketModel()
	if got := tm.Render(narrowWidth); got != "No ticket selected." {
		t.Errorf("Render(empty) = %q, want placeholder text", got)
	}
}

func TestTicketModel_Render_NilData(t *testing.T) {
	tm := &TicketModel{}
	if got := tm.Render(narrowWidth); got != "No ticket selected." {
		t.Errorf("Render(nil data) = %q, want placeholder text", got)
	}
}

func TestTicketModel_Render_MissingOptionalFields(t *testing.T) {
	tm := NewTicketModel()
	var detail client.TicketDetail
	loadFixture(t, "ticket_detail/ticket_missing_optional_fields.json", &detail)
	tm.SetData(&detail)

	got := tm.Render(narrowWidth)
	if !strings.Contains(got, "TICK-144") {
		t.Errorf("Render output missing ticket ID:\n%s", got)
	}
	// Optional fields (branch, milestone) are empty in this fixture and
	// must not produce dangling labels with no value.
	if strings.Contains(got, "Branch:") {
		t.Errorf("Render should omit Branch: line when branch is empty:\n%s", got)
	}
	if strings.Contains(got, "Milestone:") {
		t.Errorf("Render should omit Milestone: line when milestone is empty:\n%s", got)
	}
}

func TestTicketModel_Render_IncludesReviewFindings(t *testing.T) {
	tm := NewTicketModel()
	var detail client.TicketDetail
	loadFixture(t, "ticket_detail/ticket_changes_requested.json", &detail)
	tm.SetData(&detail)

	got := stripANSI(tm.Render(narrowWidth))
	if !strings.Contains(got, "changes_requested") {
		t.Errorf("Render output missing review verdict:\n%s", got)
	}
	if !strings.Contains(got, "Migration lacks downtime strategy") {
		t.Errorf("Render output missing review finding text:\n%s", got)
	}
}

// --- BlockedModel ---

func TestBlockedModel_SetDataGetData(t *testing.T) {
	bm := NewBlockedModel()
	if bm.GetData() == nil {
		t.Fatal("expected NewBlockedModel to start with non-nil data")
	}

	var payload client.BlockedPayload
	loadFixture(t, "blocked/blocked_queue.json", &payload)

	bm.SetData(&payload)
	if bm.GetData() != &payload {
		t.Error("GetData did not return the payload set via SetData")
	}
	if len(bm.GetData().Blocked) != 2 {
		t.Errorf("expected 2 blocked tickets, got %d", len(bm.GetData().Blocked))
	}
}

func TestBlockedModel_Render_Empty(t *testing.T) {
	bm := NewBlockedModel()
	var payload client.BlockedPayload
	loadFixture(t, "blocked/blocked_empty.json", &payload)
	bm.SetData(&payload)

	got := bm.Render(narrowWidth)
	if got != "No blocked tickets." {
		t.Errorf("Render(empty) = %q, want placeholder text", got)
	}
}

func TestBlockedModel_Render_NilData(t *testing.T) {
	bm := &BlockedModel{}
	if got := bm.Render(narrowWidth); got != "No blocked tickets." {
		t.Errorf("Render(nil data) = %q, want placeholder text", got)
	}
}

func TestBlockedModel_Render_ContainsFindings(t *testing.T) {
	bm := NewBlockedModel()
	var payload client.BlockedPayload
	loadFixture(t, "blocked/blocked_queue.json", &payload)
	bm.SetData(&payload)

	got := stripANSI(bm.Render(narrowWidth))
	for _, want := range []string{"TICK-145", "TICK-143", "Missing rollback testing"} {
		if !strings.Contains(got, want) {
			t.Errorf("Render output missing %q:\n%s", want, got)
		}
	}
}

func TestBlockedModel_Render_GroupsByAttentionCategory(t *testing.T) {
	bm := NewBlockedModel()
	bm.SetData(&client.BlockedPayload{Blocked: []client.BlockedTicket{
		{ID: "TICK-1", Title: "Escalated", AttentionCategory: "escalated", AttentionSummary: "Manual check required"},
		{ID: "TICK-2", Title: "Rejected", AttentionCategory: "rejected", AttentionSummary: "Reviewer found a regression", Findings: []string{"Add a regression test"}},
		{ID: "TICK-3", Title: "Stuck", AttentionCategory: "stuck", AttentionSummary: "Executor requires re-authentication"},
		{ID: "TICK-4", Title: "Awaiting merge", AttentionCategory: "awaiting_merge", AttentionSummary: "Approved; awaiting human merge decision"},
	}})

	got := stripANSI(bm.Render(narrowWidth))
	for _, want := range []string{
		"Escalated", "Changes Requested", "Stuck", "Awaiting Merge",
		"Manual check required", "Reviewer found a regression", "Add a regression test",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("Render output missing %q:\n%s", want, got)
		}
	}
	if !bm.MoveSelection(2) || bm.SelectedTicketID() != "TICK-3" {
		t.Errorf("selection after grouped navigation = %q, want TICK-3", bm.SelectedTicketID())
	}
}

// --- DiffModel ---

func TestDiffModel_SetDataGetData(t *testing.T) {
	dm := NewDiffModel()
	if dm.GetData() == nil {
		t.Fatal("expected NewDiffModel to start with non-nil data")
	}

	var payload client.DiffPayload
	loadFixture(t, "diff/diff_small.json", &payload)

	dm.SetData(&payload)
	if dm.GetData() != &payload {
		t.Error("GetData did not return the payload set via SetData")
	}
	if dm.GetData().ID != "TICK-150" {
		t.Errorf("ID = %q, want TICK-150", dm.GetData().ID)
	}
}

func TestDiffModel_Render_NilData(t *testing.T) {
	dm := &DiffModel{}
	if got := dm.Render(narrowWidth); got != "No diff available." {
		t.Errorf("Render(nil data) = %q, want placeholder text", got)
	}
}

func TestDiffModel_Render_Empty(t *testing.T) {
	dm := NewDiffModel()
	var payload client.DiffPayload
	loadFixture(t, "diff/diff_empty.json", &payload)
	dm.SetData(&payload)

	got := dm.Render(narrowWidth)
	if !strings.Contains(got, "No changed files.") {
		t.Errorf("Render output missing empty-state text:\n%s", got)
	}
}

func TestDiffModel_Render_Error(t *testing.T) {
	dm := NewDiffModel()
	dm.SetData(&client.DiffPayload{ID: "TICK-1", Error: "diff unavailable: branch not found"})

	got := stripANSI(dm.Render(narrowWidth))
	if !strings.Contains(got, "diff unavailable: branch not found") {
		t.Errorf("Render output missing error text:\n%s", got)
	}
}

func TestDiffModel_Render_BinaryFileShowsMarkerNotRawPatch(t *testing.T) {
	dm := NewDiffModel()
	var payload client.DiffPayload
	loadFixture(t, "diff/diff_binary_file.json", &payload)
	dm.SetData(&payload)

	got := dm.Render(narrowWidth)
	if !strings.Contains(got, "[binary file — no diff shown]") {
		t.Errorf("Render output missing binary marker:\n%s", got)
	}
	if strings.Contains(got, "differ") {
		t.Errorf("Render output should not dump the raw binary patch marker text:\n%s", got)
	}
}

func TestDiffModel_Render_TruncatedPatchShowsMarker(t *testing.T) {
	dm := NewDiffModel()
	var payload client.DiffPayload
	loadFixture(t, "diff/diff_truncated_patch.json", &payload)
	dm.SetData(&payload)

	got := stripANSI(dm.Render(narrowWidth))
	if !strings.Contains(got, "(patch truncated)") {
		t.Errorf("Render output missing truncated marker:\n%s", got)
	}
}

func TestDiffModel_Render_ManyFilesShowsAllPathsAndRename(t *testing.T) {
	dm := NewDiffModel()
	var payload client.DiffPayload
	loadFixture(t, "diff/diff_many_files.json", &payload)
	dm.SetData(&payload)

	got := stripANSI(dm.Render(narrowWidth))
	for _, want := range []string{"auth.py", "token.py", "legacy_auth.py", "test_auth.py", "auth_experimental.py -> src/middleware/auth_v2.py"} {
		if !strings.Contains(got, want) {
			t.Errorf("Render output missing %q:\n%s", want, got)
		}
	}
}

// --- RunModel ---

func TestRunModel_SetDataGetData(t *testing.T) {
	rm := NewRunModel()
	if rm.GetData() == nil {
		t.Fatal("expected NewRunModel to start with non-nil data")
	}

	var payload client.RunPayload
	loadFixture(t, "run/run_active.json", &payload)

	rm.SetData(&payload)
	if rm.GetData() != &payload {
		t.Error("GetData did not return the payload set via SetData")
	}
}

func TestRunModel_Render_Idle(t *testing.T) {
	rm := NewRunModel()
	var payload client.RunPayload
	loadFixture(t, "run/run_idle.json", &payload)
	rm.SetData(&payload)

	if got := rm.Render(narrowWidth); got != "No active orchestration run." {
		t.Errorf("Render(idle) = %q, want placeholder text", got)
	}
}

func TestRunModel_Render_NilData(t *testing.T) {
	rm := &RunModel{}
	if got := rm.Render(narrowWidth); got != "No active orchestration run." {
		t.Errorf("Render(nil data) = %q, want placeholder text", got)
	}
}

func TestRunModel_Render_ActiveShowsWorkerAndRunID(t *testing.T) {
	rm := NewRunModel()
	var payload client.RunPayload
	loadFixture(t, "run/run_active.json", &payload)
	rm.SetData(&payload)

	got := stripANSI(rm.Render(narrowWidth))
	for _, want := range []string{payload.RunID, "TICK-150", "RUNNING", "codex-implement", "codex-a", "gpt-5.6-terra"} {
		if !strings.Contains(got, want) {
			t.Errorf("Render output missing %q:\n%s", want, got)
		}
	}
}

func TestRunModel_Render_BetweenDispatchesShowsAnalyzePhase(t *testing.T) {
	rm := NewRunModel()
	rm.SetData(&client.RunPayload{
		RunID:           "TICK-001-1700000000-1-implement",
		Status:          "between-dispatches",
		ProcessAlive:    true,
		OrchestratorPID: 12321,
		Orchestration:   &client.Orchestration{Active: true, State: "between-dispatches"},
		Analysis: &client.AnalysisStatus{
			TicketID: "TICK-002",
			Phase:    "model_requested",
			Executor: "claude",
			Model:    "claude-haiku-4-5-20251001",
		},
	})

	got := stripANSI(rm.Render(narrowWidth))
	if strings.Contains(got, "No active orchestration run.") {
		t.Errorf("Render showed no-active-run placeholder:\n%s", got)
	}
	for _, want := range []string{"BETWEEN-DISPATCHES", "TICK-002", "Waiting for model…", "executor=claude"} {
		if !strings.Contains(got, want) {
			t.Errorf("Render output missing %q:\n%s", want, got)
		}
	}
}

func TestRunModel_Render_BatchStatus(t *testing.T) {
	rm := NewRunModel()
	reason := "selected TICK-366 has parallel_safe=false"
	rm.SetData(&client.RunPayload{
		RunID:             "run-1",
		Status:            "running",
		BatchLine:         "[orchestrate] batch: 1 running of cap 3, 2 peers (3 open tickets total)",
		UnderfilledReason: &reason,
	})

	got := stripANSI(rm.Render(narrowWidth))
	for _, want := range []string{"Batch:", "Under-filled:", reason} {
		if !strings.Contains(got, want) {
			t.Errorf("Render output missing %q:\n%s", want, got)
		}
	}

	rm.SetData(&client.RunPayload{RunID: "run-1", Status: "running"})
	got = stripANSI(rm.Render(narrowWidth))
	for _, unwanted := range []string{"Batch:", "Under-filled:"} {
		if strings.Contains(got, unwanted) {
			t.Errorf("Render output unexpectedly contains %q:\n%s", unwanted, got)
		}
	}
}

func TestRunModel_Render_ResumeWatchShowsRateLimitedInstance(t *testing.T) {
	// Without this, resume-watch's own phase/elapsed_time is instance-agnostic:
	// the Run screen would say "waiting" with no way to tell claude-a from
	// codex from a stalled run.
	rm := NewRunModel()
	rm.SetData(&client.RunPayload{
		ResumeWatchStatus: &client.ResumeWatchStatus{Phase: "waiting", ElapsedTime: 300},
		Orchestration: &client.Orchestration{
			LastCooldown: &client.CooldownEvent{
				Instance: "claude-a",
				Reason:   "rate limit or quota interruption (executor exited 1)",
			},
		},
	})

	got := stripANSI(rm.Render(narrowWidth))
	if !strings.Contains(got, "rate-limited instance: claude-a") {
		t.Errorf("Render output missing rate-limited instance line:\n%s", got)
	}
}

func TestRunModel_Render_CompletedRunHasNoActiveWorkers(t *testing.T) {
	rm := NewRunModel()
	var payload client.RunPayload
	loadFixture(t, "run/run_completed.json", &payload)
	rm.SetData(&payload)

	got := rm.Render(narrowWidth)
	if !strings.Contains(got, "(no active workers)") {
		t.Errorf("Render output missing no-workers placeholder:\n%s", got)
	}
}

// TestRunModel_Render_LiveOutcomesFillsInAsTicketsFinish is a TICK-464
// regression test: SetLiveBatchTickets must drive an incrementally-populated
// per-ticket outcome table on the active Run screen, excluding tickets that
// are still in progress and showing the failure reason once known.
func TestRunModel_Render_LiveOutcomesFillsInAsTicketsFinish(t *testing.T) {
	rm := NewRunModel()
	rm.SetData(&client.RunPayload{RunID: "run-live", Status: "running"})

	failReason := "pytest exit 1"
	rm.SetLiveBatchTickets([]client.TicketOutcome{
		{TicketID: "TICK-201", Executor: "claude-a", Outcome: "in_progress"},
		{TicketID: "TICK-202", Executor: "codex-a", Outcome: "success", DurationSeconds: 30.2},
		{TicketID: "TICK-203", Executor: "claude-b", Outcome: "failure", DurationSeconds: 5.0, FailureReason: &failReason},
	})

	got := stripANSI(rm.Render(narrowWidth))
	if !strings.Contains(got, "Live Outcomes") {
		t.Errorf("Render output missing Live Outcomes section:\n%s", got)
	}
	if strings.Contains(got, "TICK-201") {
		t.Errorf("Render output should exclude still in_progress TICK-201:\n%s", got)
	}
	for _, want := range []string{
		"TICK-202", "codex-a", "success", "30.2s",
		"TICK-203", "claude-b", "failure", "5.0s",
		"TICK-203 failure reason: pytest exit 1",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("Render output missing %q:\n%s", want, got)
		}
	}
}

// TestRunModel_Render_LiveOutcomesEmptyWhenNoTicketHasFinished ensures no
// empty "Live Outcomes" table is shown before any dispatched ticket in the
// current batch has reached an outcome.
func TestRunModel_Render_LiveOutcomesEmptyWhenNoTicketHasFinished(t *testing.T) {
	rm := NewRunModel()
	rm.SetData(&client.RunPayload{RunID: "run-live", Status: "running"})
	rm.SetLiveBatchTickets([]client.TicketOutcome{
		{TicketID: "TICK-201", Executor: "claude-a", Outcome: "in_progress"},
	})

	got := rm.Render(narrowWidth)
	if strings.Contains(got, "Live Outcomes") {
		t.Errorf("Render output should not show Live Outcomes before any ticket finishes:\n%s", got)
	}
}

func TestRunModel_AppendLogEvent_BoundsBufferToMaxLogLines(t *testing.T) {
	rm := NewRunModel()
	for i := 0; i < maxLogLines+5; i++ {
		rm.AppendLogEvent(client.LogEvent{Message: strings.Repeat("x", 1) + string(rune('a'+i%26))})
	}
	lines := rm.LogLines()
	if len(lines) != maxLogLines {
		t.Fatalf("len(LogLines()) = %d, want %d", len(lines), maxLogLines)
	}
}

func TestRunModel_AppendLogEvent_ClearsStreamError(t *testing.T) {
	rm := NewRunModel()
	rm.SetStreamError(errors.New("connection reset"))
	rm.AppendLogEvent(client.LogEvent{Message: "recovered"})

	var payload client.RunPayload
	loadFixture(t, "run/run_active.json", &payload)
	rm.SetData(&payload)

	got := rm.Render(narrowWidth)
	if strings.Contains(got, "connection reset") {
		t.Errorf("expected a successful log event to clear the prior stream error:\n%s", got)
	}
}

func TestRunModel_Render_StreamErrorSurfacedInAuditSection(t *testing.T) {
	rm := NewRunModel()
	var payload client.RunPayload
	loadFixture(t, "run/run_active.json", &payload)
	rm.SetData(&payload)
	rm.SetMode(RunModeAudit)
	rm.SetStreamError(errors.New("stream disconnected"))

	got := stripANSI(rm.Render(narrowWidth))
	if !strings.Contains(got, "Stream error:") || !strings.Contains(got, "stream disconnected") {
		t.Errorf("Render output missing stream error:\n%s", got)
	}
}

// --- RunModel Raw Audit Log live tail history (TICK-304) ---

func TestRunModel_HistoryRequest_FalseWithNoLiveTailYet(t *testing.T) {
	rm := NewRunModel()
	if _, _, ok := rm.HistoryRequest(200); ok {
		t.Error("HistoryRequest should be false before any live-tail event establishes a boundary")
	}
}

func TestRunModel_HistoryRequest_ComputesOffsetFromLiveTailBoundary(t *testing.T) {
	rm := NewRunModel()
	rm.SetData(&client.RunPayload{RunID: "run-1000"})
	// Simulate a run with 1000 events: append IDs 801..1000, which is what
	// remains in the live tail after the 200-line cap trims the rest.
	for id := 801; id <= 1000; id++ {
		rm.AppendLogEvent(client.LogEvent{ID: strconv.Itoa(id), Message: "line-" + strconv.Itoa(id)})
	}

	offset, limit, ok := rm.HistoryRequest(200)
	if !ok {
		t.Fatal("expected HistoryRequest to be true once the live tail has events")
	}
	if offset != 600 || limit != 200 {
		t.Errorf("HistoryRequest = (%d, %d), want (600, 200)", offset, limit)
	}
}

func TestRunModel_SetHistoryPage_PrependsAndAdvancesCursor(t *testing.T) {
	rm := NewRunModel()
	rm.SetData(&client.RunPayload{RunID: "run-1000"})
	for id := 801; id <= 1000; id++ {
		rm.AppendLogEvent(client.LogEvent{ID: strconv.Itoa(id), Message: "line-" + strconv.Itoa(id)})
	}

	offset, limit, ok := rm.HistoryRequest(200)
	if !ok {
		t.Fatal("expected a due history request")
	}
	lines := make([]string, limit)
	for i := range lines {
		lines[i] = "line-" + strconv.Itoa(offset+i)
	}
	rm.SetHistoryPage("run-1000", offset, lines)

	if got := rm.HistoryLines(); len(got) != 200 || got[0] != "line-600" || got[199] != "line-799" {
		t.Fatalf("HistoryLines = %v (len %d), want line-600..line-799", got, len(got))
	}
	if rm.HistoryExhausted() {
		t.Error("HistoryExhausted should be false with more history before offset 600")
	}

	// Walk backward to the start of the run.
	for {
		offset, limit, ok := rm.HistoryRequest(200)
		if !ok {
			break
		}
		page := make([]string, limit)
		for i := range page {
			page[i] = "line-" + strconv.Itoa(offset+i)
		}
		rm.SetHistoryPage("run-1000", offset, page)
	}
	if !rm.HistoryExhausted() {
		t.Error("expected HistoryExhausted once history has been paged back to offset 0")
	}
	if got := rm.HistoryLines(); len(got) != 800 || got[0] != "line-0" {
		t.Fatalf("HistoryLines after full walk-back: len=%d first=%q, want len=800 first=line-0", len(got), got[0])
	}
	// Combined with the live tail (200 lines), all 1000 events are reachable.
	if got := len(rm.HistoryLines()) + len(rm.LogLines()); got != 1000 {
		t.Errorf("HistoryLines+LogLines = %d, want 1000", got)
	}
}

func TestRunModel_SetHistoryPage_RejectsPageForDifferentRun(t *testing.T) {
	rm := NewRunModel()
	rm.SetData(&client.RunPayload{RunID: "run-current"})
	rm.SetHistoryPage("run-stale", 0, []string{"old line"})

	if err := rm.HistoryError(); err == "" {
		t.Error("expected a history error when the page's run_id doesn't match the current run")
	}
	if len(rm.HistoryLines()) != 0 {
		t.Error("a stale-run page must not be spliced into HistoryLines")
	}
}

func TestRunModel_SetData_NewRunIDResetsHistory(t *testing.T) {
	rm := NewRunModel()
	rm.SetData(&client.RunPayload{RunID: "run-a"})
	rm.SetHistoryPage("run-a", 0, []string{"from run a"})
	if len(rm.HistoryLines()) == 0 {
		t.Fatal("setup: expected history to be recorded for run-a")
	}

	rm.SetData(&client.RunPayload{RunID: "run-b"})
	if len(rm.HistoryLines()) != 0 {
		t.Error("expected a new run_id to reset stale history from the previous run")
	}
	if rm.HistoryExhausted() {
		t.Error("expected HistoryExhausted to reset for the new run")
	}
}

func TestRunModel_HistoryRequest_FalseWhileLoadingOrErrored(t *testing.T) {
	rm := NewRunModel()
	rm.SetData(&client.RunPayload{RunID: "run-1"})
	rm.AppendLogEvent(client.LogEvent{ID: "5", Message: "line-5"})

	rm.SetHistoryLoading(true)
	if _, _, ok := rm.HistoryRequest(200); ok {
		t.Error("HistoryRequest should be false while a fetch is already loading")
	}
	rm.SetHistoryLoading(false)

	rm.SetHistoryError(errors.New("boom"))
	if _, _, ok := rm.HistoryRequest(200); ok {
		t.Error("HistoryRequest should be false after a fetch error, until RetryHistory")
	}
	rm.RetryHistory()
	if _, _, ok := rm.HistoryRequest(200); !ok {
		t.Error("HistoryRequest should be true again after RetryHistory clears the error")
	}
}

func TestRunModel_ActivityRendersOnlyStructuredEvents(t *testing.T) {
	rm := NewRunModel()
	var payload client.RunPayload
	loadFixture(t, "run/run_active.json", &payload)
	rm.SetData(&payload)

	rm.SetActivityEvents("", &client.RunEventsPayload{Events: []client.ExecutorEvent{{
		Ts:       "2026-07-28T10:15:02Z",
		TicketID: "TICK-150",
		Progress: client.ExecutorProgress{Phase: "testing", Activity: "testing", Executor: "codex-a", Model: "gpt-5.6-terra", TestSummary: &client.TestSummary{Status: "pass", Passed: 12}},
	}}})
	got := stripANSI(rm.Render(narrowWidth))
	for _, want := range []string{"Activity", "TICK-150", "tests passed", "12 passed", "✓"} {
		if !strings.Contains(got, want) {
			t.Errorf("Render missing %q:\n%s", want, got)
		}
	}
	if strings.Contains(got, "raw executor protocol") {
		t.Errorf("default Activity must not expose raw audit output:\n%s", got)
	}
	if fmt.Sprint(ui.ActivityStyle(ui.ActivityCategorySuccess).GetForeground()) == fmt.Sprint(ui.ActivityStyle(ui.ActivityCategoryDanger).GetForeground()) {
		t.Error("successful and failed Activity entries must use distinct colors as well as durable labels")
	}
}

func TestRunModel_HistoricalActivityUsesStructuredEventPayload(t *testing.T) {
	rm := NewRunModel()
	var payload client.RunPayload
	loadFixture(t, "run/run_active.json", &payload)
	rm.SetData(&payload)
	var events client.RunEventsPayload
	loadFixture(t, "run/executor_events_historical.json", &events)
	rm.SetActivityEvents(events.RunID, &events)

	got := stripANSI(rm.Render(narrowWidth))
	for _, want := range []string{"Activity", "TICK-149", "claude-b/claude-sonnet-5", "completed"} {
		if !strings.Contains(got, want) {
			t.Errorf("historical activity missing %q:\n%s", want, got)
		}
	}
	if strings.Contains(got, "raw executor protocol") {
		t.Errorf("historical Activity must not expose raw audit output:\n%s", got)
	}
}

func TestRunModel_ActivityFallsBackToLifecycleWhenEventsMissing(t *testing.T) {
	rm := NewRunModel()
	var payload client.RunPayload
	loadFixture(t, "run/run_active.json", &payload)
	rm.SetData(&payload)
	rm.SetActivityEvents("", &client.RunEventsPayload{})

	got := stripANSI(rm.Render(narrowWidth))
	for _, want := range []string{"TICK-150", "RUNNING", "heartbeat count", "waiting for the first structured event"} {
		if !strings.Contains(got, want) {
			t.Errorf("fallback missing %q:\n%s", want, got)
		}
	}
}

func TestRunModel_AuditModeRendersPaginatedRawLogsOnlyWhenExplicit(t *testing.T) {
	previousProfile := lipgloss.ColorProfile()
	lipgloss.SetColorProfile(termenv.TrueColor)
	defer lipgloss.SetColorProfile(previousProfile)

	rm := NewRunModel()
	var payload client.RunPayload
	loadFixture(t, "run/run_active.json", &payload)
	rm.SetData(&payload)
	rm.SetMode(RunModeAudit)
	rm.SetAuditLogs(&client.RunLogsPayload{
		RunID: "run-active", Events: []client.LogEvent{{Message: "raw executor protocol line", Style: "bold blue"}}, TotalCount: 2, Offset: 0, Limit: 1,
	})

	rendered := rm.Render(narrowWidth)
	if !strings.Contains(rendered, "\x1b[1;34m") {
		t.Errorf("audit render did not apply API style metadata:\n%s", rendered)
	}
	got := stripANSI(rendered)
	for _, want := range []string{"Raw Audit Log", "raw executor protocol line"} {
		if !strings.Contains(got, want) {
			t.Errorf("audit render missing %q:\n%s", want, got)
		}
	}
	// Page position (Entries N-N of N / n next page) is pinned in the app-level
	// footer instead of the scrolling body — see TestUpdate_RunAuditKeyGatesRawLogsAndPages.
	if rm.AuditOffset() != 0 || rm.AuditTotal() != 2 || len(rm.AuditEvents()) != 1 {
		t.Errorf("audit pagination state = offset %d total %d events %d, want 0/2/1", rm.AuditOffset(), rm.AuditTotal(), len(rm.AuditEvents()))
	}
}

func TestRunModel_AuditModePreservesStyleInLiveTailAndHistory(t *testing.T) {
	previousProfile := lipgloss.ColorProfile()
	lipgloss.SetColorProfile(termenv.TrueColor)
	defer lipgloss.SetColorProfile(previousProfile)

	rm := NewRunModel()
	rm.SetData(&client.RunPayload{RunID: "run-active"})
	rm.SetMode(RunModeAudit)
	rm.AppendLogEvent(client.LogEvent{ID: "2", Message: "diff --git a/a b/a", Style: "bold blue"})
	rm.SetHistoryPageWithMetadata(
		"run-active", 0, []string{"@@ -1 +1 @@"}, []string{"info"}, []string{"magenta"},
	)

	rendered := rm.Render(narrowWidth)
	if !strings.Contains(rendered, "\x1b[1;34m") {
		t.Errorf("live tail did not preserve bold-blue style:\n%s", rendered)
	}
	if !strings.Contains(rendered, "\x1b[35m") {
		t.Errorf("history did not preserve magenta style:\n%s", rendered)
	}
}

func TestRunModel_AuditModePrettyPrintsJSONMessagesAndPassesNonJSONThrough(t *testing.T) {
	jsonMsg := `{"tool":"edit","args":{"file":"x.py","line":10}}`
	rm := NewRunModel()
	var payload client.RunPayload
	loadFixture(t, "run/run_active.json", &payload)
	rm.SetData(&payload)
	rm.SetMode(RunModeAudit)
	rm.SetAuditLogs(&client.RunLogsPayload{
		RunID: "run-active", Events: []client.LogEvent{{Message: jsonMsg}}, TotalCount: 1, Offset: 0, Limit: 1,
	})

	got := stripANSI(rm.Render(narrowWidth))
	if !strings.Contains(got, "\"tool\"") || !strings.Contains(got, "\"args\"") {
		t.Errorf("expected pretty-printed JSON fragments present:\n%s", got)
	}
	lines := strings.Split(got, "\n")
	toolIdx, argsIdx := -1, -1
	for i, line := range lines {
		if strings.Contains(line, "\"tool\"") {
			toolIdx = i
		}
		if strings.Contains(line, "\"args\"") {
			argsIdx = i
		}
	}
	if toolIdx == -1 || argsIdx == -1 || toolIdx == argsIdx {
		t.Errorf("expected \"tool\" and \"args\" on separate lines (indented multi-line output), got:\n%s", got)
	}

	rmPlain := NewRunModel()
	rmPlain.SetData(&payload)
	rmPlain.SetMode(RunModeAudit)
	plainMsg := "raw executor protocol line"
	rmPlain.SetAuditLogs(&client.RunLogsPayload{
		RunID: "run-active", Events: []client.LogEvent{{Message: plainMsg}}, TotalCount: 1, Offset: 0, Limit: 1,
	})
	gotPlain := stripANSI(rmPlain.Render(narrowWidth))
	if !strings.Contains(gotPlain, ui.WrapText(plainMsg, narrowWidth)) {
		t.Errorf("expected non-JSON message to render via unchanged WrapText output:\n%s", gotPlain)
	}
}

func TestRunModelAuditSemanticFormatting(t *testing.T) {
	previousProfile := lipgloss.ColorProfile()
	lipgloss.SetColorProfile(termenv.TrueColor)
	defer lipgloss.SetColorProfile(previousProfile)

	rm := NewRunModel()
	rm.SetData(&client.RunPayload{RunID: "run-active"})
	rm.SetMode(RunModeAudit)
	rm.SetAuditLogs(&client.RunLogsPayload{
		RunID: "run-active",
		Events: []client.LogEvent{
			{Message: "executor failed (exit 1)", Level: "error"},
			{Message: "executor finished (exit 0)", Level: "success"},
			{Message: "executor retrying (attempt 2)", Level: "warning"},
		},
		TotalCount: 3,
		Limit:      3,
	})

	rendered := rm.Render(narrowWidth)
	if !strings.Contains(rendered, "\x1b[31m✗") {
		t.Errorf("error audit entry missing red ANSI styling:\n%s", rendered)
	}
	if !strings.Contains(rendered, "\x1b[32m✓") {
		t.Errorf("success audit entry missing green ANSI styling:\n%s", rendered)
	}
	// The audit glyph set is unified with ActivitySymbol/ActivityStyle
	// (TICK-478), so a warning-level entry renders "~" in the waiting
	// color rather than the audit-only "!" glyph it used before.
	if !strings.Contains(rendered, "\x1b[33m~") {
		t.Errorf("warning audit entry missing unified activity glyph/styling:\n%s", rendered)
	}
	if got := formatAuditEvent(client.LogEvent{Message: "diff --git a/a b/a", Level: "info", Style: "bold blue"}, narrowWidth); !strings.Contains(got, "\x1b[1;34m") {
		t.Errorf("style metadata did not override info-level styling:\n%s", got)
	}
	if copied, count := rm.CopyText(); count != 3 || copied != "executor failed (exit 1)\nexecutor finished (exit 0)\nexecutor retrying (attempt 2)" {
		t.Errorf("CopyText = %q (%d items), want unmodified messages", copied, count)
	}

	rmHistory := NewRunModel()
	rmHistory.SetData(&client.RunPayload{RunID: "run-active"})
	rmHistory.SetMode(RunModeAudit)
	rmHistory.SetHistoryPageWithLevels(
		"run-active", 0, []string{"older executor failed (exit 1)"}, []string{"error"},
	)
	historyRendered := rmHistory.Render(narrowWidth)
	if !strings.Contains(historyRendered, "\x1b[31m✗") {
		t.Errorf("historical audit entry missing red ANSI styling:\n%s", historyRendered)
	}
}

func TestRunModel_Render_HistoryAndSelectedRunDetail(t *testing.T) {
	rm := NewRunModel()
	failReason := "pytest exit 1"
	revReason := "needs operational docs"
	rm.SetHistory(&client.RunHistoryPayload{
		Runs: []client.RunSummaryPayload{
			{
				RunID:     "run-20260730T100000Z-11111111",
				Timestamp: "2026-07-30T10:00:00Z",
				Reason:    "failure",
				BatchTickets: []client.TicketOutcome{
					{
						TicketID:        "TICK-101",
						Executor:        "claude-a",
						Outcome:         "failure",
						DurationSeconds: 42.5,
						FailureReason:   &failReason,
					},
					{
						TicketID:        "TICK-102",
						Executor:        "codex-a",
						Outcome:         "changes_requested",
						DurationSeconds: 15.0,
						ReviewReason:    &revReason,
					},
				},
			},
			{
				RunID:     "run-20260729T090000Z-22222222",
				Timestamp: "2026-07-29T09:00:00Z",
				Reason:    "success",
				BatchTickets: []client.TicketOutcome{
					{
						TicketID:        "TICK-100",
						Executor:        "claude-a",
						Outcome:         "success",
						DurationSeconds: 60.0,
					},
				},
			},
		},
	})

	if got := stripANSI(rm.Render(narrowWidth)); strings.Contains(got, "Run History") {
		t.Errorf("Render should be live-run-only, got history output:\n%s", got)
	}
	got := stripANSI(rm.RenderHistory(narrowWidth))
	for _, want := range []string{
		"Run History",
		"TYPE",
		"LANE",
		"TICKETS",
		"run-20260730T100000Z-11111111",
		"TICK-101,TICK-102",
		"FAILURE",
		"Selected Run: run-20260730T100000Z-11111111",
		"Terminal Reason: failure",
		"TICK-101",
		"claude-a",
		"failure",
		"42.5s",
		"TICK-101 failure reason: pytest exit 1",
		"TICK-102 review reason: needs operational docs",
		"1 failure · 1 changes_requested",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("Render output missing %q:\n%s", want, got)
		}
	}

	if !rm.OpenSelectedHistory() {
		t.Fatal("OpenSelectedHistory returned false")
	}
	detail := stripANSI(rm.RenderHistory(narrowWidth))
	for _, want := range []string{
		"TICK-101 failure reason: pytest exit 1",
		"TICK-102 review reason: needs operational docs",
	} {
		if !strings.Contains(detail, want) {
			t.Errorf("historical detail missing %q:\n%s", want, detail)
		}
	}
}

// TestRunModel_HistoricalDetail_ShowsLiveWorkersWhenSelectedRunIsCurrent
// covers a run that appears in the Run History list before it has finished
// (e.g. terminal_reason "running"/"between-dispatches"): opening its detail
// view must still surface the live Workers/Resolved Dispatch/Batch info
// from rm.data, not just the (yet-incomplete) BatchTickets outcome summary.
func TestRunModel_HistoricalDetail_ShowsLiveWorkersWhenSelectedRunIsCurrent(t *testing.T) {
	rm := NewRunModel()
	rm.SetData(&client.RunPayload{
		RunID:  "run-live",
		Status: "between-dispatches",
		Workers: []client.RunWorker{
			{TicketID: "TICK-468", ExecutorPID: 3428693, State: "between-dispatches", ReconciliationState: "orchestrator_live", ResolvedDriver: "codex-implement", ResolvedExecutor: "claude-b", ResolvedModel: "claude-sonnet-5"},
		},
		BatchLine: "[orchestrate] batch: 1 running of cap 3, 2 peers (3 open tickets total)",
	})
	rm.SetHistory(&client.RunHistoryPayload{Runs: []client.RunSummaryPayload{{
		RunID:  "run-live",
		Reason: "running",
		BatchTickets: []client.TicketOutcome{
			{TicketID: "TICK-464", Executor: "claude-b", Outcome: "skipped", DurationSeconds: 830.0},
		},
	}}})

	if !rm.OpenSelectedHistory() {
		t.Fatal("OpenSelectedHistory returned false")
	}
	detail := stripANSI(rm.RenderHistory(narrowWidth))
	for _, want := range []string{
		"Workers",
		"TICK-468",
		"3428693",
		"Resolved Dispatch",
		"route=codex-implement executor=claude-b model=claude-sonnet-5",
		"Batch:",
	} {
		if !strings.Contains(detail, want) {
			t.Errorf("historical detail of live run missing %q:\n%s", want, detail)
		}
	}
}

// TestRunModel_RenderHistorySection_LargeBatchKeepsReasonAndOutcomesVisible
// covers a regression where a run with many dispatched tickets (a common
// batch size on this project) made the TICKETS column so wide that the
// REASON/OUTCOMES columns were pushed past the terminal width — Table.Render
// doesn't wrap or truncate rows to fit, so those columns silently scrolled
// out of view instead of erroring.
func TestRunModel_RenderHistorySection_LargeBatchKeepsReasonAndOutcomesVisible(t *testing.T) {
	rm := NewRunModel()
	tickets := make([]client.TicketOutcome, 0, 17)
	for i := 1; i <= 17; i++ {
		tickets = append(tickets, client.TicketOutcome{
			TicketID: fmt.Sprintf("TICK-%d", 400+i),
			Executor: "claude-b",
			Outcome:  "success",
		})
	}
	rm.SetHistory(&client.RunHistoryPayload{Runs: []client.RunSummaryPayload{{
		RunID:        "run-large-batch",
		Timestamp:    "2026-07-30T10:00:00Z",
		Reason:       "stopped",
		BatchTickets: tickets,
	}}})

	got := stripANSI(rm.RenderHistory(narrowWidth))
	lines := strings.Split(got, "\n")
	// The table's STARTED column now shows a formatted local timestamp
	// rather than the raw run id (which the detail lines below the table
	// still echo verbatim), so find the row by its all-caps REASON — the
	// table renders "STOPPED" while "Terminal Reason:" below renders the
	// lowercase raw value, keeping this match unique to the table row.
	var row string
	for _, line := range lines {
		if strings.Contains(line, "STOPPED") {
			row = line
			break
		}
	}
	if row == "" {
		t.Fatalf("history table row for run-large-batch not found:\n%s", got)
	}
	if !strings.Contains(row, "STOPPED") {
		t.Errorf("history row REASON column missing/pushed out of view: %q", row)
	}
	if !strings.Contains(row, "17 success") {
		t.Errorf("history row OUTCOMES column missing/pushed out of view: %q", row)
	}
	if !strings.Contains(row, "+13 more") {
		t.Errorf("history row TICKETS column should be truncated with a count, got: %q", row)
	}
}

func TestRunModel_SetHistory_PreservesSelectionByRunIDWhenListShifts(t *testing.T) {
	rm := NewRunModel()
	rm.SetHistory(&client.RunHistoryPayload{Runs: []client.RunSummaryPayload{{RunID: "run-1"}, {RunID: "run-2"}}})
	if !rm.MoveSelection(1) {
		t.Fatal("MoveSelection did not select run-2")
	}
	rm.SetHistory(&client.RunHistoryPayload{Runs: []client.RunSummaryPayload{
		{RunID: "run-3"}, {RunID: "run-1"}, {RunID: "run-2"},
	}})
	if selected := rm.SelectedRun(); selected == nil || selected.RunID != "run-2" {
		t.Errorf("SelectedRun after list shift = %#v, want run-2", selected)
	}
}

func TestRunModel_MoveSelection(t *testing.T) {
	rm := NewRunModel()
	rm.SetHistory(&client.RunHistoryPayload{
		Runs: []client.RunSummaryPayload{
			{RunID: "run-1", Reason: "success"},
			{RunID: "run-2", Reason: "failure"},
		},
	})

	if rm.SelectedIndex() != 0 {
		t.Errorf("SelectedIndex = %d, want 0", rm.SelectedIndex())
	}
	if !rm.MoveSelection(1) {
		t.Error("MoveSelection(1) returned false, want true")
	}
	if rm.SelectedIndex() != 1 {
		t.Errorf("SelectedIndex = %d, want 1", rm.SelectedIndex())
	}
	if rm.SelectedRun().RunID != "run-2" {
		t.Errorf("SelectedRun().RunID = %q, want run-2", rm.SelectedRun().RunID)
	}
	if rm.MoveSelection(1) {
		t.Error("MoveSelection(1) past end returned true, want false")
	}
}

func TestHistoryRunType_DistinguishesResumeWatchFromManualLane(t *testing.T) {
	if got := historyRunType("run-123", "resume-watch"); got != "AUTO" {
		t.Errorf("historyRunType(run-123, resume-watch) = %q, want AUTO", got)
	}
	if got := historyRunType("run-123", "manual"); got != "LANE" {
		t.Errorf("historyRunType(run-123, manual) = %q, want LANE", got)
	}
	if got := historyRunType("action-20260812T100000Z", "manual"); got != "MANUAL" {
		t.Errorf("historyRunType(action-..., manual) = %q, want MANUAL", got)
	}
	if got := historyRunType("action-20260812T100000Z", "resume-watch"); got != "MANUAL" {
		t.Errorf("historyRunType(action-..., resume-watch) = %q, want MANUAL", got)
	}

	rm := NewRunModel()
	rm.SetHistory(&client.RunHistoryPayload{
		Runs: []client.RunSummaryPayload{
			{
				RunID:       "run-20260815T120000Z",
				Timestamp:   "2026-08-15T12:00:00Z",
				Reason:      "success",
				TriggeredBy: "resume-watch",
				BatchTickets: []client.TicketOutcome{
					{TicketID: "TICK-001", Outcome: "success"},
				},
			},
		},
	})
	rendered := rm.RenderHistoryTable(80)
	if !strings.Contains(rendered, "AUTO") {
		t.Errorf("RenderHistoryTable for resume-watch run missing AUTO in output:\n%s", rendered)
	}
}

// --- SettingsModel ---

func TestSettingsModel_SetDataGetData(t *testing.T) {
	sm := NewSettingsModel()
	if sm.GetData() == nil {
		t.Fatal("expected NewSettingsModel to start with non-nil data")
	}

	var payload client.SettingsPayload
	loadFixture(t, "settings/settings_basic.json", &payload)

	sm.SetData(&payload)
	if sm.GetData() != &payload {
		t.Error("GetData did not return the payload set via SetData")
	}
}

func TestSettingsModel_Render_NilData(t *testing.T) {
	sm := &SettingsModel{}
	if got := sm.Render(narrowWidth); got != "No settings available." {
		t.Errorf("Render(nil data) = %q, want placeholder text", got)
	}
}

func TestSettingsModel_Render_Basic(t *testing.T) {
	sm := NewSettingsModel()
	var payload client.SettingsPayload
	loadFixture(t, "settings/settings_basic.json", &payload)
	sm.SetData(&payload)

	got := stripANSI(sm.Render(narrowWidth))
	for _, want := range []string{payload.RepoRoot, "claude", "staging", "127.0.0.1:8000"} {
		if !strings.Contains(got, want) {
			t.Errorf("Render output missing %q:\n%s", want, got)
		}
	}
}

func TestSettingsModel_Render_MissingOptionalFieldsOmitsEmptySections(t *testing.T) {
	sm := NewSettingsModel()
	var payload client.SettingsPayload
	loadFixture(t, "settings/settings_missing_optional.json", &payload)
	sm.SetData(&payload)

	got := sm.Render(narrowWidth)
	if strings.Contains(got, "Milestone:") {
		t.Errorf("Render should omit Milestone: line when default_milestone is empty:\n%s", got)
	}
	if strings.Contains(got, "Models") {
		t.Errorf("Render should omit the Models section when empty:\n%s", got)
	}
	if strings.Contains(got, "Environments") {
		t.Errorf("Render should omit the Environments section when empty:\n%s", got)
	}
}

func TestSettingsModel_Render_MultiExecutorShowsAllExecutors(t *testing.T) {
	sm := NewSettingsModel()
	var payload client.SettingsPayload
	loadFixture(t, "settings/settings_multi_executor.json", &payload)
	sm.SetData(&payload)

	got := stripANSI(sm.Render(narrowWidth))
	for _, want := range []string{"claude-1", "codex-1", "production"} {
		if !strings.Contains(got, want) {
			t.Errorf("Render output missing %q:\n%s", want, got)
		}
	}
}

func TestSettingsModel_Render_Error(t *testing.T) {
	sm := NewSettingsModel()
	sm.SetError(errors.New("connection refused"))

	got := sm.Render(narrowWidth)
	if !strings.Contains(got, "connection refused") {
		t.Errorf("Render output missing error text:\n%s", got)
	}
}

func TestSettingsModel_SetData_ClearsPriorError(t *testing.T) {
	sm := NewSettingsModel()
	sm.SetError(errors.New("boom"))

	var payload client.SettingsPayload
	loadFixture(t, "settings/settings_basic.json", &payload)
	sm.SetData(&payload)

	if got := sm.Render(narrowWidth); strings.Contains(got, "boom") {
		t.Errorf("expected SetData to clear a prior fetch error:\n%s", got)
	}
}

// --- SettingsModel pools reorder (TICK-269) ---

func loadPoolsFixture(t *testing.T, relPath string) []client.Pool {
	t.Helper()
	var payload client.PoolsPayload
	loadFixture(t, relPath, &payload)
	return payload.Pools
}

func TestSettingsModel_SetPools_Render_ShowsPoolsAndExecutors(t *testing.T) {
	sm := NewSettingsModel()
	var payload client.SettingsPayload
	loadFixture(t, "settings/settings_basic.json", &payload)
	sm.SetData(&payload)
	sm.SetPools(loadPoolsFixture(t, "pools/pools_basic.json"))

	got := stripANSI(sm.Render(narrowWidth))
	for _, want := range []string{"default", "least-loaded", "claude-1", "claude-2", "local", "round-robin", "ollama-a"} {
		if !strings.Contains(got, want) {
			t.Errorf("Render output missing %q:\n%s", want, got)
		}
	}
}

func TestSettingsModel_Render_NoPoolsOmitsSection(t *testing.T) {
	sm := NewSettingsModel()
	var payload client.SettingsPayload
	loadFixture(t, "settings/settings_basic.json", &payload)
	sm.SetData(&payload)

	got := sm.Render(narrowWidth)
	if strings.Contains(got, "Pools") {
		t.Errorf("Render should omit the Pools section when no pools are configured:\n%s", got)
	}
}

func TestSettingsModel_SetPoolsError_Render(t *testing.T) {
	sm := NewSettingsModel()
	var payload client.SettingsPayload
	loadFixture(t, "settings/settings_basic.json", &payload)
	sm.SetData(&payload)
	sm.SetPoolsError(errors.New("connection refused"))

	got := sm.Render(narrowWidth)
	if !strings.Contains(got, "connection refused") {
		t.Errorf("Render output missing pools fetch error:\n%s", got)
	}
}

func TestSettingsModel_MovePoolSelection(t *testing.T) {
	sm := NewSettingsModel()
	sm.SetPools(loadPoolsFixture(t, "pools/pools_basic.json"))

	if p, ok := sm.SelectedPool(); !ok || p.Name != "default" {
		t.Fatalf("expected initial selection to be 'default', got %+v (ok=%v)", p, ok)
	}
	if !sm.MovePoolSelection(1) {
		t.Fatal("expected MovePoolSelection(1) to move")
	}
	if p, ok := sm.SelectedPool(); !ok || p.Name != "local" {
		t.Fatalf("expected selection to be 'local' after moving, got %+v (ok=%v)", p, ok)
	}
	if sm.MovePoolSelection(1) {
		t.Error("expected MovePoolSelection to clamp at the last pool")
	}
}

func TestSettingsModel_BeginPoolEdit_NoPoolsReturnsFalse(t *testing.T) {
	sm := NewSettingsModel()
	if sm.BeginPoolEdit() {
		t.Error("expected BeginPoolEdit to fail with no pools loaded")
	}
}

func TestSettingsModel_PoolReorder_MoveAndCommit(t *testing.T) {
	sm := NewSettingsModel()
	sm.SetPools(loadPoolsFixture(t, "pools/pools_basic.json"))

	if !sm.BeginPoolEdit() {
		t.Fatal("expected BeginPoolEdit to succeed")
	}
	if !sm.IsEditingPool() {
		t.Fatal("expected IsEditingPool to be true after BeginPoolEdit")
	}
	if got := sm.PendingOrder(); len(got) != 2 || got[0] != "claude-1" || got[1] != "claude-2" {
		t.Fatalf("unexpected initial pending order: %v", got)
	}

	if !sm.MoveExecutorSelection(1) {
		t.Fatal("expected MoveExecutorSelection(1) to move onto claude-2")
	}
	if !sm.MoveExecutor(-1) {
		t.Fatal("expected MoveExecutor(-1) to swap claude-2 above claude-1")
	}
	got := sm.PendingOrder()
	if len(got) != 2 || got[0] != "claude-2" || got[1] != "claude-1" {
		t.Fatalf("expected reordered pending order [claude-2 claude-1], got %v", got)
	}

	// Out-of-bounds move is a no-op.
	if sm.MoveExecutor(-1) {
		t.Error("expected MoveExecutor(-1) at the top row to be a no-op")
	}

	sm.CommitPoolEdit(got)
	if sm.IsEditingPool() {
		t.Error("expected CommitPoolEdit to end the reorder")
	}
	pool, ok := sm.SelectedPool()
	if !ok || len(pool.Executors) != 2 || pool.Executors[0] != "claude-2" {
		t.Fatalf("expected committed pool executors [claude-2 claude-1], got %+v", pool)
	}
}

func TestSettingsModel_CancelPoolEdit_DiscardsChanges(t *testing.T) {
	sm := NewSettingsModel()
	sm.SetPools(loadPoolsFixture(t, "pools/pools_basic.json"))
	sm.BeginPoolEdit()
	sm.MoveExecutor(1)
	sm.CancelPoolEdit()

	if sm.IsEditingPool() {
		t.Error("expected CancelPoolEdit to end the reorder")
	}
	pool, ok := sm.SelectedPool()
	if !ok || pool.Executors[0] != "claude-1" {
		t.Fatalf("expected CancelPoolEdit to leave the original order untouched, got %+v", pool)
	}
}

func TestSettingsModel_SetPools_WhileEditingAbandonsEdit(t *testing.T) {
	sm := NewSettingsModel()
	sm.SetPools(loadPoolsFixture(t, "pools/pools_basic.json"))
	sm.BeginPoolEdit()

	sm.SetPools(loadPoolsFixture(t, "pools/pools_basic.json"))
	if sm.IsEditingPool() {
		t.Error("expected a fresh SetPools to abandon an in-progress edit")
	}
}

// --- Viewport geometry regression (TICK-303) ---
//
// view.go sizes every content viewport as
// ui.ContentHeight(terminalHeight, "\n\n"+statusBar.Render(width)) so the
// footer View() appends below the viewport (a blank join line plus the
// status bar) is reserved by its actual rendered line count. The previous
// hardcoded "terminalHeight - 2, floored at 1" reservation broke at very
// constrained terminal heights (1-2 rows): flooring at 1 meant body(1) +
// footer(2) = 3 rendered rows even though the terminal only had 1 or 2,
// pushing rows — including the final content line — off-screen. These
// tests exercise ui.ContentHeight directly the same way view.go does.
func TestViewportFinalLineVisible_ConstrainedHeight(t *testing.T) {
	var lines []string
	for i := 0; i < 40; i++ {
		lines = append(lines, fmt.Sprintf("line-%02d", i))
	}
	content := strings.Join(lines, "\n")
	finalLine := lines[len(lines)-1]

	statusBar := ui.NewStatusBar().Render(narrowWidth)
	footer := "\n\n" + statusBar

	for _, totalHeight := range []int{1, 2, 3, 4, 8, 15, 30} {
		bodyHeight := ui.ContentHeight(totalHeight, footer)

		vp := ui.NewViewport(narrowWidth, bodyHeight)
		vp.SetContent(content)
		vp.End()
		bodyOut := vp.Render()

		// Mirrors app.Model.View(): a "" body skips the "\n\n" separator
		// rather than rendering it as two rows for zero lines of content.
		var out string
		if bodyOut == "" {
			out = statusBar
		} else {
			out = bodyOut + "\n\n" + statusBar
		}

		totalRows := strings.Count(out, "\n") + 1
		if totalRows > totalHeight {
			t.Errorf("totalHeight=%d: rendered %d rows, want <= %d\n%s", totalHeight, totalRows, totalHeight, out)
		}

		if bodyOut == "" {
			// No room for content at all once the footer is reserved.
			continue
		}
		if !strings.Contains(bodyOut, finalLine) {
			t.Errorf("totalHeight=%d (bodyHeight=%d): final line %q not visible after End(); got:\n%s", totalHeight, bodyHeight, finalLine, bodyOut)
		}
	}
}

// --- Golden/snapshot tests: narrow terminal widths, empty/error states ---
//
// These cover all three screens at narrowWidth (48 cols), per TICK-156's
// close criteria. Run with -update to (re)generate testdata/*.golden after
// an intentional rendering change.

func TestGolden_BoardBasicNarrow(t *testing.T) {
	bm := NewBoardModel()
	var payload client.BoardPayload
	loadFixture(t, "board/board_basic.json", &payload)
	bm.SetData(&payload)

	compareGolden(t, "board_basic_narrow.golden", stripANSI(bm.Render(narrowWidth)))
}

func TestGolden_BoardEmptyNarrow(t *testing.T) {
	bm := NewBoardModel()
	var payload client.BoardPayload
	loadFixture(t, "board/board_empty.json", &payload)
	bm.SetData(&payload)

	compareGolden(t, "board_empty_narrow.golden", stripANSI(bm.Render(narrowWidth)))
}

func TestGolden_TicketDetailNarrow(t *testing.T) {
	tm := NewTicketModel()
	var detail client.TicketDetail
	loadFixture(t, "ticket_detail/ticket_changes_requested.json", &detail)
	tm.SetData(&detail)

	compareGolden(t, "ticket_detail_narrow.golden", stripANSI(tm.Render(narrowWidth)))
}

func TestGolden_BlockedQueueNarrow(t *testing.T) {
	bm := NewBlockedModel()
	var payload client.BlockedPayload
	loadFixture(t, "blocked/blocked_queue.json", &payload)
	bm.SetData(&payload)

	compareGolden(t, "blocked_queue_narrow.golden", stripANSI(bm.Render(narrowWidth)))
}

func TestGolden_BlockedEmptyNarrow(t *testing.T) {
	bm := NewBlockedModel()
	var payload client.BlockedPayload
	loadFixture(t, "blocked/blocked_empty.json", &payload)
	bm.SetData(&payload)

	compareGolden(t, "blocked_empty_narrow.golden", stripANSI(bm.Render(narrowWidth)))
}

// TestGolden_ErrorStateNarrow snapshots the shared error-state treatment
// (ui.StatusBar with an error set) that all three screens use to surface a
// failed fetch — e.g. the "ticket not found" error returned by
// GET /api/tickets/{id} in errors/error_missing_ticket.json.
func TestGolden_ErrorStateNarrow(t *testing.T) {
	var errPayload client.ErrorPayload
	loadFixture(t, "errors/error_missing_ticket.json", &errPayload)

	sb := ui.NewStatusBar()
	sb.SetScreen("Ticket")
	sb.SetError(errPayload.Error)
	compareGolden(t, "error_state_narrow.golden", stripANSI(sb.Render(narrowWidth)))
}

// --- Golden/snapshot tests: Diff, Run, Settings (screens 4-6, TICK-157) ---

func TestGolden_DiffTruncatedNarrow(t *testing.T) {
	dm := NewDiffModel()
	var payload client.DiffPayload
	loadFixture(t, "diff/diff_truncated_patch.json", &payload)
	dm.SetData(&payload)

	compareGolden(t, "diff_truncated_narrow.golden", stripANSI(dm.Render(narrowWidth)))
}

func TestGolden_DiffBinaryNarrow(t *testing.T) {
	dm := NewDiffModel()
	var payload client.DiffPayload
	loadFixture(t, "diff/diff_binary_file.json", &payload)
	dm.SetData(&payload)

	compareGolden(t, "diff_binary_narrow.golden", stripANSI(dm.Render(narrowWidth)))
}

func TestGolden_DiffEmptyNarrow(t *testing.T) {
	dm := NewDiffModel()
	var payload client.DiffPayload
	loadFixture(t, "diff/diff_empty.json", &payload)
	dm.SetData(&payload)

	compareGolden(t, "diff_empty_narrow.golden", stripANSI(dm.Render(narrowWidth)))
}

func TestGolden_DiffManyFilesScrolledNarrow(t *testing.T) {
	dm := NewDiffModel()
	var payload client.DiffPayload
	loadFixture(t, "diff/diff_many_files.json", &payload)
	dm.SetData(&payload)

	vp := ui.NewViewport(narrowWidth, 10)
	vp.SetContent(dm.Render(narrowWidth))
	vp.SetOffset(9)

	compareGolden(t, "diff_many_files_scrolled_narrow.golden", stripANSI(vp.Render()))
}

// TestGolden_RunActiveNarrow snapshots the default structured Activity pane
// using the same bounded /events fixture consumed by the client.
func TestGolden_RunActiveNarrow(t *testing.T) {
	// Started: is rendered in the machine's local timezone (ui.FormatLocalTS),
	// so pin time.Local for the duration of this test — otherwise the golden
	// file would only match on a machine configured for this same zone.
	loc, err := time.LoadLocation("America/Los_Angeles")
	if err != nil {
		t.Fatalf("LoadLocation: %v", err)
	}
	origLocal := time.Local
	time.Local = loc
	t.Cleanup(func() { time.Local = origLocal })

	rm := NewRunModel()
	var payload client.RunPayload
	loadFixture(t, "run/run_active.json", &payload)
	rm.SetData(&payload)

	var events client.RunEventsPayload
	loadFixture(t, "run/executor_events_live.json", &events)
	rm.SetActivityEvents("", &events)

	compareGolden(t, "run_active_narrow.golden", stripANSI(rm.Render(narrowWidth)))
}

func TestGolden_RunHistoryMixedOutcomesNarrow(t *testing.T) {
	rm := NewRunModel()
	rm.SetHistory(&client.RunHistoryPayload{Runs: []client.RunSummaryPayload{{
		RunID:     "run-mixed",
		Timestamp: "2026-07-30T10:00:00Z",
		Reason:    "stopped",
		BatchTickets: []client.TicketOutcome{
			{TicketID: "TICK-1", Executor: "claude-a", Outcome: "success", DurationSeconds: 10},
			{TicketID: "TICK-2", Executor: "claude-a", Outcome: "failure", DurationSeconds: 20},
			{TicketID: "TICK-3", Executor: "codex-a", Outcome: "changes_requested", DurationSeconds: 30},
		},
	}}})
	compareGolden(t, "run_history_mixed_outcomes_narrow.golden", stripANSI(rm.RenderHistory(narrowWidth)))
}

func TestGolden_RunIdleNarrow(t *testing.T) {
	rm := NewRunModel()
	var payload client.RunPayload
	loadFixture(t, "run/run_idle.json", &payload)
	rm.SetData(&payload)

	compareGolden(t, "run_idle_narrow.golden", stripANSI(rm.Render(narrowWidth)))
}

// TestGolden_RunErrorNarrow snapshots the run-log stream error treatment: no
// active run snapshot plus a stream failure, matching what the screen shows
// if GET /api/runs/current returns idle while the SSE stream itself errors.
func TestGolden_RunErrorNarrow(t *testing.T) {
	rm := NewRunModel()
	var payload client.RunPayload
	loadFixture(t, "run/run_idle.json", &payload)
	rm.SetData(&payload)
	rm.SetStreamError(errors.New("connection reset by peer"))

	compareGolden(t, "run_error_narrow.golden", stripANSI(rm.Render(narrowWidth)))
}

func TestGolden_SettingsBasicNarrow(t *testing.T) {
	sm := NewSettingsModel()
	var payload client.SettingsPayload
	loadFixture(t, "settings/settings_basic.json", &payload)
	sm.SetData(&payload)

	compareGolden(t, "settings_basic_narrow.golden", stripANSI(sm.Render(narrowWidth)))
}

func TestGolden_SettingsMissingOptionalNarrow(t *testing.T) {
	sm := NewSettingsModel()
	var payload client.SettingsPayload
	loadFixture(t, "settings/settings_missing_optional.json", &payload)
	sm.SetData(&payload)

	compareGolden(t, "settings_missing_optional_narrow.golden", stripANSI(sm.Render(narrowWidth)))
}

func TestGolden_SettingsErrorNarrow(t *testing.T) {
	var errPayload client.ErrorPayload
	loadFixture(t, "errors/error_missing_ticket.json", &errPayload)

	sm := NewSettingsModel()
	sm.SetError(errors.New(errPayload.Error))

	compareGolden(t, "settings_error_narrow.golden", stripANSI(sm.Render(narrowWidth)))
}

// TestGolden_SettingsPoolsNarrow covers the pools.<name>.executors reorder
// view (TICK-269): both pools rendered, dispatch counts shown, and the
// focused pool/executor cursor on the first row.
func TestGolden_SettingsPoolsNarrow(t *testing.T) {
	sm := NewSettingsModel()
	var payload client.SettingsPayload
	loadFixture(t, "settings/settings_basic.json", &payload)
	sm.SetData(&payload)
	sm.SetPools(loadPoolsFixture(t, "pools/pools_basic.json"))

	compareGolden(t, "settings_pools_narrow.golden", stripANSI(sm.Render(narrowWidth)))
}

// TestGolden_SettingsPoolsEditingNarrow covers the reorder-in-progress view:
// pendingOrder rendered in place of the saved order, with the executor
// cursor and hint text swapped for the editing variant.
func TestGolden_SettingsPoolsEditingNarrow(t *testing.T) {
	sm := NewSettingsModel()
	var payload client.SettingsPayload
	loadFixture(t, "settings/settings_basic.json", &payload)
	sm.SetData(&payload)
	sm.SetPools(loadPoolsFixture(t, "pools/pools_basic.json"))
	sm.BeginPoolEdit()
	sm.MoveExecutorSelection(1)
	sm.MoveExecutor(-1)

	compareGolden(t, "settings_pools_editing_narrow.golden", stripANSI(sm.Render(narrowWidth)))
}

// --- Golden tests: Resume Watch daemon status (TICK-169) ---

// TestGolden_RunResumeWatchAbsentNarrow covers the idle run screen with no
// resume-watch daemon present: the widget must be absent from the output.
func TestGolden_RunResumeWatchAbsentNarrow(t *testing.T) {
	rm := NewRunModel()
	var payload client.RunPayload
	loadFixture(t, "run/resume_watch_absent.json", &payload)
	rm.SetData(&payload)

	compareGolden(t, "run_resume_watch_absent_narrow.golden", stripANSI(rm.Render(narrowWidth)))
}

// TestGolden_RunResumeWatchWaitingNarrow covers the idle run screen with the
// daemon alive and waiting between retries.
func TestGolden_RunResumeWatchWaitingNarrow(t *testing.T) {
	rm := NewRunModel()
	var payload client.RunPayload
	loadFixture(t, "run/resume_watch_waiting.json", &payload)
	rm.SetData(&payload)

	got := stripANSI(rm.Render(narrowWidth))
	if !strings.Contains(got, "Resume Watch") {
		t.Errorf("Render output missing 'Resume Watch' section:\n%s", got)
	}
	if !strings.Contains(got, "waiting") {
		t.Errorf("Render output missing 'waiting' phase:\n%s", got)
	}
	compareGolden(t, "run_resume_watch_waiting_narrow.golden", got)
}

// TestGolden_RunResumeWatchGaveUpNarrow covers the idle run screen with the
// daemon having given up after hitting the ceiling.
func TestGolden_RunResumeWatchGaveUpNarrow(t *testing.T) {
	rm := NewRunModel()
	var payload client.RunPayload
	loadFixture(t, "run/resume_watch_gave_up.json", &payload)
	rm.SetData(&payload)

	got := stripANSI(rm.Render(narrowWidth))
	if !strings.Contains(got, "Resume Watch") {
		t.Errorf("Render output missing 'Resume Watch' section:\n%s", got)
	}
	if !strings.Contains(got, "gave up") {
		t.Errorf("Render output missing 'gave up' text:\n%s", got)
	}
	compareGolden(t, "run_resume_watch_gave_up_narrow.golden", got)
}
