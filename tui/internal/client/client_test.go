package client

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"testing"
)

// fixturesRoot resolves the shared Python/Go fixture corpus at
// tests/fixtures/tui_contracts, relative to this source file rather than the
// test binary's working directory, so it works regardless of how `go test`
// is invoked.
func fixturesRoot(t *testing.T) string {
	t.Helper()
	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("could not resolve caller for fixturesRoot")
	}
	// this file: tui/internal/client/client_test.go
	root := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "tests", "fixtures", "tui_contracts")
	if _, err := os.Stat(root); err != nil {
		t.Fatalf("fixtures root not found at %s: %v", root, err)
	}
	return root
}

// --- FixtureClient ---

func TestNewFixtureClient_RequiresDir(t *testing.T) {
	if _, err := NewFixtureClient(""); err == nil {
		t.Fatal("expected error for empty fixture directory")
	}
}

func TestNewFixtureClient_RejectsMissingDir(t *testing.T) {
	if _, err := NewFixtureClient("/no/such/directory/really"); err == nil {
		t.Fatal("expected error for nonexistent fixture directory")
	}
}

func TestNewFixtureClient_RejectsNonDirectory(t *testing.T) {
	f, err := os.CreateTemp(t.TempDir(), "not-a-dir")
	if err != nil {
		t.Fatalf("create temp file: %v", err)
	}
	f.Close()

	if _, err := NewFixtureClient(f.Name()); err == nil {
		t.Fatal("expected error when fixture path is a file, not a directory")
	}
}

func TestFixtureClient_GetBoard(t *testing.T) {
	fc, err := NewFixtureClient(fixturesRoot(t))
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	board, err := fc.GetBoard(context.Background())
	if err != nil {
		t.Fatalf("GetBoard: %v", err)
	}

	openTickets, ok := board.Tickets["open"]
	if !ok || len(openTickets) == 0 {
		t.Fatalf("expected an 'open' status column with tickets, got %+v", board.Tickets)
	}
	if openTickets[0].ID != "TICK-150" {
		t.Errorf("first open ticket = %q, want TICK-150", openTickets[0].ID)
	}
	if len(board.Pipeline) != 1 || board.Pipeline[0].Env != "staging" {
		t.Errorf("unexpected pipeline: %+v", board.Pipeline)
	}
}

func TestFixtureClient_GetBoard_MissingFixture(t *testing.T) {
	dir := t.TempDir()
	fc, err := NewFixtureClient(dir)
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	if _, err := fc.GetBoard(context.Background()); err == nil {
		t.Fatal("expected error when board fixture file is missing")
	}
}

func TestFixtureClient_GetTickets(t *testing.T) {
	fc, err := NewFixtureClient(fixturesRoot(t))
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	tickets, err := fc.GetTickets(context.Background())
	if err != nil {
		t.Fatalf("GetTickets: %v", err)
	}
	if len(tickets.Tickets) != 5 {
		t.Fatalf("expected 5 flat tickets, got %d", len(tickets.Tickets))
	}
	if tickets.Tickets[0].ID != "TICK-150" {
		t.Errorf("first ticket = %q, want TICK-150", tickets.Tickets[0].ID)
	}
}

func TestFixtureClient_GetTicketDetail_FallsBackWhenIDFileMissing(t *testing.T) {
	fc, err := NewFixtureClient(fixturesRoot(t))
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	// No fixture file is named after a raw ticket ID (they're named
	// descriptively, e.g. ticket_ready.json), so any ID here should fall
	// back to ticket_detail/ticket_ready.json.
	detail, err := fc.GetTicketDetail(context.Background(), "TICK-999")
	if err != nil {
		t.Fatalf("GetTicketDetail: %v", err)
	}
	if detail.ID != "TICK-150" {
		t.Errorf("expected fallback to ticket_ready fixture (TICK-150), got %q", detail.ID)
	}
}

func TestFixtureClient_GetTicketDetail_UsesIDFileWhenPresent(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "ticket_detail"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.MkdirAll(filepath.Join(dir, "board"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	payload := `{"id":"TICK-777","title":"Direct hit","status":"open"}`
	if err := os.WriteFile(filepath.Join(dir, "ticket_detail", "TICK-777.json"), []byte(payload), 0o644); err != nil {
		t.Fatalf("write fixture: %v", err)
	}

	fc, err := NewFixtureClient(dir)
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	detail, err := fc.GetTicketDetail(context.Background(), "TICK-777")
	if err != nil {
		t.Fatalf("GetTicketDetail: %v", err)
	}
	if detail.ID != "TICK-777" || detail.Title != "Direct hit" {
		t.Errorf("unexpected detail: %+v", detail)
	}
}

func TestFixtureClient_GetBlocked(t *testing.T) {
	fc, err := NewFixtureClient(fixturesRoot(t))
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	blocked, err := fc.GetBlocked(context.Background())
	if err != nil {
		t.Fatalf("GetBlocked: %v", err)
	}
	if len(blocked.Blocked) != 2 {
		t.Fatalf("expected 2 blocked tickets, got %d", len(blocked.Blocked))
	}
	if blocked.Blocked[0].ID != "TICK-145" {
		t.Errorf("first blocked ticket = %q, want TICK-145", blocked.Blocked[0].ID)
	}
}

func TestFixtureClient_GetBlocked_EmptyNormalizesToEmptySlice(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "blocked"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "blocked", "blocked_queue.json"), []byte(`{"blocked":[]}`), 0o644); err != nil {
		t.Fatalf("write fixture: %v", err)
	}

	fc, err := NewFixtureClient(dir)
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	blocked, err := fc.GetBlocked(context.Background())
	if err != nil {
		t.Fatalf("GetBlocked: %v", err)
	}
	if blocked.Blocked == nil {
		t.Error("expected Blocked to normalize to an empty slice, got nil")
	}
	if len(blocked.Blocked) != 0 {
		t.Errorf("expected 0 blocked tickets, got %d", len(blocked.Blocked))
	}
}

func TestFixtureClient_GetDiff(t *testing.T) {
	fc, err := NewFixtureClient(fixturesRoot(t))
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	diff, err := fc.GetDiff(context.Background(), "TICK-999")
	if err != nil {
		t.Fatalf("GetDiff: %v", err)
	}
	if diff.ID != "TICK-150" {
		t.Errorf("expected fallback to diff_small fixture (TICK-150), got %q", diff.ID)
	}
}

func TestFixtureClient_GetDiff_UsesIDFileWhenPresent(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "diff"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	payload := `{"id":"TICK-777","ticket_id":"TICK-777","files":[]}`
	if err := os.WriteFile(filepath.Join(dir, "diff", "TICK-777.json"), []byte(payload), 0o644); err != nil {
		t.Fatalf("write fixture: %v", err)
	}

	fc, err := NewFixtureClient(dir)
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	diff, err := fc.GetDiff(context.Background(), "TICK-777")
	if err != nil {
		t.Fatalf("GetDiff: %v", err)
	}
	if diff.ID != "TICK-777" {
		t.Errorf("unexpected diff: %+v", diff)
	}
}

func TestFixtureClient_GetDiff_FilesNilNormalizedToEmptySlice(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "diff"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "diff", "diff_small.json"), []byte(`{"id":"TICK-1"}`), 0o644); err != nil {
		t.Fatalf("write fixture: %v", err)
	}

	fc, err := NewFixtureClient(dir)
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	diff, err := fc.GetDiff(context.Background(), "TICK-1")
	if err != nil {
		t.Fatalf("GetDiff: %v", err)
	}
	if diff.Files == nil {
		t.Error("expected Files to normalize to an empty slice, got nil")
	}
}

func TestFixtureClient_GetCurrentRun(t *testing.T) {
	fc, err := NewFixtureClient(fixturesRoot(t))
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	run, err := fc.GetCurrentRun(context.Background())
	if err != nil {
		t.Fatalf("GetCurrentRun: %v", err)
	}
	if run.RunID == "" {
		t.Error("expected a non-empty run id from run_active.json")
	}
	if len(run.Workers) != 1 || run.Workers[0].TicketID != "TICK-150" {
		t.Errorf("unexpected workers: %+v", run.Workers)
	}
}

func TestFixtureClient_GetSettings(t *testing.T) {
	fc, err := NewFixtureClient(fixturesRoot(t))
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	settings, err := fc.GetSettings(context.Background())
	if err != nil {
		t.Fatalf("GetSettings: %v", err)
	}
	if settings.RepoRoot == "" {
		t.Error("expected a non-empty repo_root from settings_basic.json")
	}
	if settings.Executor != "claude" {
		t.Errorf("Executor = %q, want claude", settings.Executor)
	}
}

func TestFixtureClient_GetPools(t *testing.T) {
	fc, err := NewFixtureClient(fixturesRoot(t))
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	pools, err := fc.GetPools(context.Background())
	if err != nil {
		t.Fatalf("GetPools: %v", err)
	}
	if len(pools.Pools) != 2 {
		t.Fatalf("expected 2 pools from pools_basic.json, got %d", len(pools.Pools))
	}
	if pools.Pools[0].Name != "default" || len(pools.Pools[0].Executors) != 2 {
		t.Errorf("unexpected first pool: %+v", pools.Pools[0])
	}
}

func TestFixtureClient_GetPools_EmptyNormalizedToEmptySlice(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "pools"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "pools", "pools_basic.json"), []byte(`{}`), 0o644); err != nil {
		t.Fatalf("write fixture: %v", err)
	}

	fc, err := NewFixtureClient(dir)
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	pools, err := fc.GetPools(context.Background())
	if err != nil {
		t.Fatalf("GetPools: %v", err)
	}
	if pools.Pools == nil {
		t.Error("expected Pools to normalize to an empty slice, got nil")
	}
}

func TestFixtureClient_UpdatePoolExecutors_EchoesRequestedOrder(t *testing.T) {
	fc, err := NewFixtureClient(fixturesRoot(t))
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	pool, err := fc.UpdatePoolExecutors(context.Background(), "default", []string{"claude-2", "claude-1"})
	if err != nil {
		t.Fatalf("UpdatePoolExecutors: %v", err)
	}
	if pool.Name != "default" {
		t.Errorf("Name = %q, want default", pool.Name)
	}
	want := []string{"claude-2", "claude-1"}
	if len(pool.Executors) != len(want) || pool.Executors[0] != want[0] || pool.Executors[1] != want[1] {
		t.Errorf("Executors = %v, want %v", pool.Executors, want)
	}
}

func TestFixtureClient_UpdatePoolExecutors_UnknownPoolErrors(t *testing.T) {
	fc, err := NewFixtureClient(fixturesRoot(t))
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	if _, err := fc.UpdatePoolExecutors(context.Background(), "nonexistent", []string{"x"}); err == nil {
		t.Fatal("expected error for unknown pool")
	}
}

func TestFixtureClient_OpenRunLogStream(t *testing.T) {
	fc, err := NewFixtureClient(fixturesRoot(t))
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	rc, err := fc.OpenRunLogStream(context.Background(), "")
	if err != nil {
		t.Fatalf("OpenRunLogStream: %v", err)
	}
	defer rc.Close()

	events, err := ParseSSE(rc)
	if err != nil {
		t.Fatalf("ParseSSE: %v", err)
	}
	if len(events) != 3 {
		t.Fatalf("expected 3 events from events_basic.sse, got %d", len(events))
	}
	if events[0].ID != "1" || events[0].Type != "log" {
		t.Errorf("unexpected first event: %+v", events[0])
	}
}

func TestFixtureClient_RunActivityAndAuditFixtures(t *testing.T) {
	fc, err := NewFixtureClient(fixturesRoot(t))
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	live, err := fc.GetRunEvents(context.Background(), "current")
	if err != nil || len(live.Events) == 0 {
		t.Fatalf("GetRunEvents(current) = %+v, %v; want live fixture events", live, err)
	}
	historical, err := fc.GetRunEvents(context.Background(), "run-historical")
	if err != nil || len(historical.Events) == 0 {
		t.Fatalf("GetRunEvents(historical) = %+v, %v; want historical fixture events", historical, err)
	}
	audit, err := fc.GetRunLogs(context.Background(), "current", 0, 1)
	if err != nil || len(audit.Events) != 1 || audit.TotalCount < 2 || audit.NextOffset == nil {
		t.Fatalf("GetRunLogs = %+v, %v; want first paginated audit fixture event", audit, err)
	}
	if got := audit.Events[0]; got.Level != "info" || got.Kind != "executor" {
		t.Errorf("audit event metadata = level %q, kind %q; want info, executor", got.Level, got.Kind)
	}
	if got := audit.Events[0]; got.Style != "dim" {
		t.Errorf("audit event style = %q, want dim", got.Style)
	}

	auditErr, err := fc.GetRunLogs(context.Background(), "current", 2, 1)
	if err != nil || len(auditErr.Events) != 1 {
		t.Fatalf("GetRunLogs error event = %+v, %v; want third paginated audit fixture event", auditErr, err)
	}
	if got := auditErr.Events[0]; got.Level != "error" || got.Kind != "executor" {
		t.Errorf("audit error event metadata = level %q, kind %q; want error, executor", got.Level, got.Kind)
	}
	if got := auditErr.Events[0]; got.Style != "bold red" {
		t.Errorf("audit error event style = %q, want bold red", got.Style)
	}

	basicPage, err := fc.GetRunLogPage(context.Background(), 0, 10)
	if err != nil || len(basicPage.Events) == 0 {
		t.Fatalf("GetRunLogPage = %+v, %v; want basic sse fixture events", basicPage, err)
	}
	if got := basicPage.Events[0]; got.Level != "info" || got.Kind != "orchestrator" {
		t.Errorf("basic sse event metadata = level %q, kind %q; want info, orchestrator", got.Level, got.Kind)
	}
	if got := basicPage.Events[0]; got.Style != "dim" {
		t.Errorf("basic sse event style = %q, want dim", got.Style)
	}
	if len(basicPage.Events) < 3 {
		t.Fatalf("expected at least 3 basic sse fixture events, got %d", len(basicPage.Events))
	}
	if got := basicPage.Events[2]; got.Style != "bold blue" {
		t.Errorf("basic sse third event style = %q, want bold blue", got.Style)
	}
}

// --- HTTPClient ---

func TestNewHTTPClient_ValidatesScheme(t *testing.T) {
	if _, err := NewHTTPClient("example.com"); err == nil {
		t.Fatal("expected error for URL missing scheme")
	}
	if _, err := NewHTTPClient("ftp://example.com"); err == nil {
		t.Fatal("expected error for non-http(s) scheme")
	}
}

func TestNewHTTPClient_TrimsTrailingSlash(t *testing.T) {
	c, err := NewHTTPClient("http://127.0.0.1:8000/")
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}
	if c.baseURL != "http://127.0.0.1:8000" {
		t.Errorf("baseURL = %q, want trailing slash trimmed", c.baseURL)
	}
}

func TestHTTPClient_GetBoard(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/board" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(BoardPayload{
			Tickets: map[string][]Ticket{
				"open": {{ID: "TICK-1", Title: "First", Status: "open"}},
			},
			Pipeline: []PipelineEntry{},
		})
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	board, err := c.GetBoard(context.Background())
	if err != nil {
		t.Fatalf("GetBoard: %v", err)
	}
	if len(board.Tickets["open"]) != 1 || board.Tickets["open"][0].ID != "TICK-1" {
		t.Errorf("unexpected board: %+v", board.Tickets)
	}
}

func TestHTTPClient_GetBoard_NilFieldsNormalized(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{}`))
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	board, err := c.GetBoard(context.Background())
	if err != nil {
		t.Fatalf("GetBoard: %v", err)
	}
	if board.Tickets == nil {
		t.Error("expected Tickets map to be normalized to non-nil")
	}
	if board.Pipeline == nil {
		t.Error("expected Pipeline slice to be normalized to non-nil")
	}
}

func TestHTTPClient_GetTickets(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/tickets" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		json.NewEncoder(w).Encode(TicketsPayload{Tickets: []Ticket{{ID: "TICK-2"}}})
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	tickets, err := c.GetTickets(context.Background())
	if err != nil {
		t.Fatalf("GetTickets: %v", err)
	}
	if len(tickets.Tickets) != 1 || tickets.Tickets[0].ID != "TICK-2" {
		t.Errorf("unexpected tickets: %+v", tickets.Tickets)
	}
}

func TestHTTPClient_GetTicketDetail_RequiresID(t *testing.T) {
	c, err := NewHTTPClient("http://127.0.0.1:8000")
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}
	if _, err := c.GetTicketDetail(context.Background(), ""); err == nil {
		t.Fatal("expected error for empty ticket ID")
	}
}

func TestHTTPClient_GetTicketDetail_Success(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/tickets/TICK-150" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		json.NewEncoder(w).Encode(TicketDetail{ID: "TICK-150", Title: "Auth refactor"})
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	detail, err := c.GetTicketDetail(context.Background(), "TICK-150")
	if err != nil {
		t.Fatalf("GetTicketDetail: %v", err)
	}
	if detail.ID != "TICK-150" {
		t.Errorf("detail.ID = %q, want TICK-150", detail.ID)
	}
}

func TestHTTPClient_GetTicketDetail_NotFound(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		json.NewEncoder(w).Encode(ErrorPayload{Error: "ticket TICK-999 not found"})
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	_, err = c.GetTicketDetail(context.Background(), "TICK-999")
	if err == nil {
		t.Fatal("expected error for 404 response")
	}
	if !strings.Contains(err.Error(), "ticket TICK-999 not found") {
		t.Errorf("error = %q, want it to contain the API error message", err.Error())
	}
}

func TestHTTPClient_GetBlocked(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/blocked" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		json.NewEncoder(w).Encode(BlockedPayload{Blocked: []BlockedTicket{{ID: "TICK-145"}}})
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	blocked, err := c.GetBlocked(context.Background())
	if err != nil {
		t.Fatalf("GetBlocked: %v", err)
	}
	if len(blocked.Blocked) != 1 || blocked.Blocked[0].ID != "TICK-145" {
		t.Errorf("unexpected blocked payload: %+v", blocked.Blocked)
	}
}

func TestHTTPClient_ServerError_NonJSONBody(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		w.Write([]byte("internal server error"))
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	_, err = c.GetBoard(context.Background())
	if err == nil {
		t.Fatal("expected error for HTTP 500 response")
	}
	if !strings.Contains(err.Error(), "500") {
		t.Errorf("error = %q, want it to mention the status code", err.Error())
	}
}

func TestHTTPClient_GetDiff_RequiresID(t *testing.T) {
	c, err := NewHTTPClient("http://127.0.0.1:8000")
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}
	if _, err := c.GetDiff(context.Background(), ""); err == nil {
		t.Fatal("expected error for empty ticket ID")
	}
}

func TestHTTPClient_GetDiff_Success(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/diff/TICK-150" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		json.NewEncoder(w).Encode(DiffPayload{ID: "TICK-150", Files: []DiffFile{{Path: "a.py"}}})
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	diff, err := c.GetDiff(context.Background(), "TICK-150")
	if err != nil {
		t.Fatalf("GetDiff: %v", err)
	}
	if diff.ID != "TICK-150" || len(diff.Files) != 1 {
		t.Errorf("unexpected diff: %+v", diff)
	}
}

func TestHTTPClient_GetDiff_NilFilesNormalized(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"id":"TICK-1"}`))
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	diff, err := c.GetDiff(context.Background(), "TICK-1")
	if err != nil {
		t.Fatalf("GetDiff: %v", err)
	}
	if diff.Files == nil {
		t.Error("expected Files to normalize to an empty slice, got nil")
	}
}

func TestHTTPClient_GetCurrentRun(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/runs/current" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		json.NewEncoder(w).Encode(RunPayload{RunID: "run-1", Status: "running"})
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	run, err := c.GetCurrentRun(context.Background())
	if err != nil {
		t.Fatalf("GetCurrentRun: %v", err)
	}
	if run.RunID != "run-1" {
		t.Errorf("RunID = %q, want run-1", run.RunID)
	}
	if run.Workers == nil || run.Tickets == nil {
		t.Errorf("expected Workers/Tickets to normalize to empty slices, got %+v", run)
	}
}

func TestHTTPClient_GetRunHistory(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/runs" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		json.NewEncoder(w).Encode(RunHistoryPayload{
			Runs: []RunSummaryPayload{
				{
					RunID:     "run-100",
					Timestamp: "2026-07-30T10:00:00Z",
					Reason:    "success",
					BatchTickets: []TicketOutcome{
						{
							TicketID:        "TICK-100",
							Executor:        "claude-a",
							Outcome:         "success",
							DurationSeconds: 30.5,
						},
					},
				},
			},
		})
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	history, err := c.GetRunHistory(context.Background())
	if err != nil {
		t.Fatalf("GetRunHistory: %v", err)
	}
	if len(history.Runs) != 1 {
		t.Fatalf("len(Runs) = %d, want 1", len(history.Runs))
	}
	if history.Runs[0].RunID != "run-100" || history.Runs[0].Reason != "success" {
		t.Errorf("unexpected run payload: %+v", history.Runs[0])
	}
}

func TestHTTPClient_GetRunSummary(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/runs/run-100" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		failReason := "pytest exit 1"
		json.NewEncoder(w).Encode(RunSummaryPayload{
			RunID:     "run-100",
			Timestamp: "2026-07-30T10:00:00Z",
			Reason:    "failure",
			BatchTickets: []TicketOutcome{
				{
					TicketID:        "TICK-101",
					Executor:        "codex-a",
					Outcome:         "failure",
					DurationSeconds: 15.0,
					FailureReason:   &failReason,
				},
			},
		})
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	summary, err := c.GetRunSummary(context.Background(), "run-100")
	if err != nil {
		t.Fatalf("GetRunSummary: %v", err)
	}
	if summary.RunID != "run-100" || summary.Reason != "failure" {
		t.Errorf("unexpected summary payload: %+v", summary)
	}
	if len(summary.BatchTickets) != 1 || summary.BatchTickets[0].FailureReason == nil || *summary.BatchTickets[0].FailureReason != "pytest exit 1" {
		t.Errorf("unexpected batch tickets: %+v", summary.BatchTickets)
	}
}

func TestHTTPClient_GetSettings(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/config" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		json.NewEncoder(w).Encode(SettingsPayload{RepoRoot: "/repo", Executor: "claude"})
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	settings, err := c.GetSettings(context.Background())
	if err != nil {
		t.Fatalf("GetSettings: %v", err)
	}
	if settings.RepoRoot != "/repo" {
		t.Errorf("RepoRoot = %q, want /repo", settings.RepoRoot)
	}
	if settings.Environments == nil {
		t.Error("expected Environments to normalize to an empty slice, got nil")
	}
}

func TestHTTPClient_GetPools(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/pools" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		json.NewEncoder(w).Encode(PoolsPayload{
			Pools: []Pool{
				{Name: "default", Strategy: "least-loaded", Executors: []string{"claude-1", "claude-2"}},
			},
		})
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	pools, err := c.GetPools(context.Background())
	if err != nil {
		t.Fatalf("GetPools: %v", err)
	}
	if len(pools.Pools) != 1 || pools.Pools[0].Name != "default" {
		t.Errorf("unexpected pools payload: %+v", pools)
	}
}

func TestHTTPClient_GetPools_NilFieldsNormalized(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{}`))
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	pools, err := c.GetPools(context.Background())
	if err != nil {
		t.Fatalf("GetPools: %v", err)
	}
	if pools.Pools == nil {
		t.Error("expected Pools to normalize to an empty slice, got nil")
	}
}

func TestHTTPClient_UpdatePoolExecutors_SendsPUTWithBody(t *testing.T) {
	var gotMethod, gotPath string
	var gotBody map[string][]string

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod = r.Method
		gotPath = r.URL.Path
		if err := json.NewDecoder(r.Body).Decode(&gotBody); err != nil {
			t.Errorf("decode request body: %v", err)
		}
		json.NewEncoder(w).Encode(Pool{
			Name:      "default",
			Strategy:  "least-loaded",
			Executors: gotBody["executors"],
		})
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	pool, err := c.UpdatePoolExecutors(context.Background(), "default", []string{"claude-2", "claude-1"})
	if err != nil {
		t.Fatalf("UpdatePoolExecutors: %v", err)
	}

	if gotMethod != http.MethodPut {
		t.Errorf("method = %q, want PUT", gotMethod)
	}
	if gotPath != "/api/pools/default/executors" {
		t.Errorf("path = %q, want /api/pools/default/executors", gotPath)
	}
	want := []string{"claude-2", "claude-1"}
	if len(gotBody["executors"]) != len(want) || gotBody["executors"][0] != want[0] {
		t.Errorf("request body executors = %v, want %v", gotBody["executors"], want)
	}
	if len(pool.Executors) != 2 || pool.Executors[0] != "claude-2" {
		t.Errorf("unexpected returned pool: %+v", pool)
	}
}

func TestHTTPClient_UpdatePoolExecutors_ErrorStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		json.NewEncoder(w).Encode(ErrorPayload{Error: "executors must be a reordering"})
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	if _, err := c.UpdatePoolExecutors(context.Background(), "default", []string{"claude-1"}); err == nil {
		t.Fatal("expected error for HTTP 400 response")
	}
}

func TestHTTPClient_OpenRunLogStream_Success(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/runs/current/logs/stream" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.Write([]byte("id: 1\nevent: log\ndata: {\"id\":\"1\",\"message\":\"hi\"}\n\n"))
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	rc, err := c.OpenRunLogStream(context.Background(), "")
	if err != nil {
		t.Fatalf("OpenRunLogStream: %v", err)
	}
	defer rc.Close()

	events, err := ParseSSE(rc)
	if err != nil {
		t.Fatalf("ParseSSE: %v", err)
	}
	if len(events) != 1 || events[0].ID != "1" {
		t.Errorf("unexpected events: %+v", events)
	}
}

func TestHTTPClient_OpenRunLogStream_ErrorStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
		w.Write([]byte("no active run"))
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	if _, err := c.OpenRunLogStream(context.Background(), ""); err == nil {
		t.Fatal("expected error for non-2xx stream response")
	}
}

func TestHTTPClient_GetRunLogPage_SendsOffsetAndLimit(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/runs/current/logs" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		if got := r.URL.Query().Get("offset"); got != "600" {
			t.Errorf("offset query = %q, want 600", got)
		}
		if got := r.URL.Query().Get("limit"); got != "200" {
			t.Errorf("limit query = %q, want 200", got)
		}
		next := 800
		json.NewEncoder(w).Encode(LogPagePayload{
			RunID:      "run-1000",
			Offset:     600,
			Limit:      200,
			TotalCount: 1000,
			NextOffset: &next,
			Events: []LogEvent{
				{ID: "601", Type: "log", RunID: "run-1000", Message: "line-600"},
				{ID: "602", Type: "log", RunID: "run-1000", Message: "line-601"},
			},
		})
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	page, err := c.GetRunLogPage(context.Background(), 600, 200)
	if err != nil {
		t.Fatalf("GetRunLogPage: %v", err)
	}
	if page.TotalCount != 1000 {
		t.Errorf("TotalCount = %d, want 1000", page.TotalCount)
	}
	if page.NextOffset == nil || *page.NextOffset != 800 {
		t.Errorf("NextOffset = %v, want 800", page.NextOffset)
	}
	if len(page.Events) != 2 || page.Events[0].Message != "line-600" {
		t.Errorf("unexpected events: %+v", page.Events)
	}
}

func TestHTTPClient_GetRunLogPage_NotFound(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		w.Write([]byte(`{"error": "no log file available"}`))
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	if _, err := c.GetRunLogPage(context.Background(), 0, 200); err == nil {
		t.Fatal("expected error for 404 response")
	}
}

func TestHTTPClient_GetRunLogPage_EventsNilNormalizedToEmptySlice(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(LogPagePayload{RunID: "run-1", TotalCount: 0})
	}))
	defer srv.Close()

	c, err := NewHTTPClient(srv.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	page, err := c.GetRunLogPage(context.Background(), 0, 200)
	if err != nil {
		t.Fatalf("GetRunLogPage: %v", err)
	}
	if page.Events == nil {
		t.Error("expected Events to normalize to an empty slice, got nil")
	}
}

func TestFixtureClient_GetRunLogPage_SlicesEventsBySequentialOffset(t *testing.T) {
	fc, err := NewFixtureClient(fixturesRoot(t))
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}

	first, err := fc.GetRunLogPage(context.Background(), 0, 2)
	if err != nil {
		t.Fatalf("GetRunLogPage: %v", err)
	}
	if len(first.Events) == 0 {
		t.Fatal("expected at least one event from the events_basic.sse fixture")
	}
	if first.NextOffset == nil {
		t.Fatal("expected NextOffset to be set when more events remain")
	}

	rest, err := fc.GetRunLogPage(context.Background(), *first.NextOffset, first.TotalCount)
	if err != nil {
		t.Fatalf("GetRunLogPage: %v", err)
	}
	if rest.NextOffset != nil {
		t.Errorf("NextOffset = %v, want nil once the fixture is fully paged", rest.NextOffset)
	}
	if first.TotalCount != rest.TotalCount {
		t.Errorf("TotalCount should be stable across pages: first=%d rest=%d", first.TotalCount, rest.TotalCount)
	}
	if got, want := len(first.Events)+len(rest.Events), first.TotalCount; got != want {
		t.Errorf("combined event count = %d, want %d (TotalCount)", got, want)
	}
}

func TestFixtureClient_GetRunLogPage_PreservesSemanticMetadata(t *testing.T) {
	fixtureDir := t.TempDir()
	runDir := filepath.Join(fixtureDir, "run")
	if err := os.Mkdir(runDir, 0o755); err != nil {
		t.Fatalf("Mkdir run fixture directory: %v", err)
	}
	fixture := "id: 1\nevent: log\ndata: {\"id\":\"1\",\"type\":\"log\",\"run_id\":\"run-1\",\"message\":\"executor failed\",\"level\":\"error\",\"kind\":\"executor\"}\n\n"
	if err := os.WriteFile(filepath.Join(runDir, "events_basic.sse"), []byte(fixture), 0o644); err != nil {
		t.Fatalf("Write events_basic.sse: %v", err)
	}

	fc, err := NewFixtureClient(fixtureDir)
	if err != nil {
		t.Fatalf("NewFixtureClient: %v", err)
	}
	page, err := fc.GetRunLogPage(context.Background(), 0, 1)
	if err != nil {
		t.Fatalf("GetRunLogPage: %v", err)
	}
	if len(page.Events) != 1 {
		t.Fatalf("events = %d, want 1", len(page.Events))
	}
	if got := page.Events[0]; got.Level != "error" || got.Kind != "executor" {
		t.Errorf("event metadata = level %q, kind %q; want error, executor", got.Level, got.Kind)
	}
}

// --- SSE parsing ---

func TestParseSSE_ParsesMultipleFrames(t *testing.T) {
	body := "id: 1\nevent: log\ndata: {\"a\":1}\n\nid: 2\nevent: log\ndata: {\"a\":2}\n\n"
	events, err := ParseSSE(strings.NewReader(body))
	if err != nil {
		t.Fatalf("ParseSSE: %v", err)
	}
	if len(events) != 2 {
		t.Fatalf("expected 2 events, got %d", len(events))
	}
	if events[0].ID != "1" || events[1].ID != "2" {
		t.Errorf("unexpected event ids: %+v", events)
	}
}

func TestParseSSE_MultilineData(t *testing.T) {
	body := "data: line one\ndata: line two\n\n"
	events, err := ParseSSE(strings.NewReader(body))
	if err != nil {
		t.Fatalf("ParseSSE: %v", err)
	}
	if len(events) != 1 || events[0].Data != "line one\nline two" {
		t.Errorf("unexpected events: %+v", events)
	}
}

func TestParseSSE_TruncatedFrameStillReturned(t *testing.T) {
	// No trailing blank line: the frame ends with EOF instead of the usual
	// terminator, which happens with a truncated fixture file.
	body := "id: 1\nevent: log\ndata: {\"a\":1}"
	events, err := ParseSSE(strings.NewReader(body))
	if err != nil {
		t.Fatalf("ParseSSE: %v", err)
	}
	if len(events) != 1 || events[0].ID != "1" {
		t.Errorf("expected the truncated frame to still be returned, got %+v", events)
	}
}

func TestDecodeLogEvent(t *testing.T) {
	ev, err := DecodeLogEvent(SSEEvent{ID: "1", Type: "log", Data: `{"id":"1","message":"hello"}`})
	if err != nil {
		t.Fatalf("DecodeLogEvent: %v", err)
	}
	if ev.Message != "hello" {
		t.Errorf("Message = %q, want hello", ev.Message)
	}
}

func TestDecodeLogEvent_Style(t *testing.T) {
	ev, err := DecodeLogEvent(SSEEvent{ID: "1", Type: "log", Data: `{"id":"1","message":"@@ -1,2 +1,2 @@","style":"bold red"}`})
	if err != nil {
		t.Fatalf("DecodeLogEvent: %v", err)
	}
	if ev.Style != "bold red" {
		t.Errorf("Style = %q, want %q", ev.Style, "bold red")
	}
}

func TestDecodeLogEvent_EmptyData(t *testing.T) {
	ev, err := DecodeLogEvent(SSEEvent{})
	if err != nil {
		t.Fatalf("DecodeLogEvent: %v", err)
	}
	if ev.ID != "" || ev.Type != "" || ev.Message != "" || ev.Data != nil {
		t.Errorf("expected zero LogEvent for empty data, got %+v", ev)
	}
}

func TestIsNewerEventID(t *testing.T) {
	tests := []struct {
		id, last string
		want     bool
	}{
		{"1", "", true},
		{"", "1", true},
		{"2", "1", true},
		{"1", "2", false},
		{"1", "1", false},
		{"a", "b", true},
		{"a", "a", false},
	}
	for _, tt := range tests {
		if got := isNewerEventID(tt.id, tt.last); got != tt.want {
			t.Errorf("isNewerEventID(%q, %q) = %v, want %v", tt.id, tt.last, got, tt.want)
		}
	}
}

// --- ReconnectingStream ---

func newFrameReader(id, message string) io.ReadCloser {
	body := "id: " + id + "\nevent: log\ndata: {\"id\":\"" + id + "\",\"message\":\"" + message + "\"}\n\n"
	return io.NopCloser(bytes.NewReader([]byte(body)))
}

func TestReconnectingStream_ReconnectsOnDrop(t *testing.T) {
	var opens int
	opener := func(ctx context.Context, lastEventID string) (io.ReadCloser, error) {
		opens++
		switch opens {
		case 1:
			return newFrameReader("1", "first"), nil
		case 2:
			return newFrameReader("2", "second"), nil
		default:
			return io.NopCloser(strings.NewReader("")), nil
		}
	}

	rs := NewReconnectingStream(context.Background(), opener)

	ev, err := rs.Next()
	if err != nil {
		t.Fatalf("Next (1): %v", err)
	}
	if ev.Message != "first" {
		t.Errorf("first event = %+v, want message=first", ev)
	}

	// The first stream is exhausted after one frame; Next should transparently
	// reconnect (via opener) and return the second stream's event rather than
	// propagating the EOF to the caller.
	ev, err = rs.Next()
	if err != nil {
		t.Fatalf("Next (2, after reconnect): %v", err)
	}
	if ev.Message != "second" {
		t.Errorf("second event = %+v, want message=second", ev)
	}
	if opens != 2 {
		t.Errorf("opener calls = %d, want 2 (initial connect + one reconnect)", opens)
	}
}

func TestReconnectingStream_GivesUpAfterFailedReconnect(t *testing.T) {
	var opens int
	opener := func(ctx context.Context, lastEventID string) (io.ReadCloser, error) {
		opens++
		if opens == 1 {
			return newFrameReader("1", "first"), nil
		}
		return nil, errors.New("connection refused")
	}

	rs := NewReconnectingStream(context.Background(), opener)

	if _, err := rs.Next(); err != nil {
		t.Fatalf("Next (1): %v", err)
	}
	if _, err := rs.Next(); err == nil {
		t.Fatal("expected an error once the stream drops and reconnect also fails")
	}
}

func TestReconnectingStream_SkipsReplayedEventsAfterReconnect(t *testing.T) {
	// A reconnect that replays the whole log from the start (as the real
	// server does) must not re-deliver an event already seen before the
	// drop.
	var opens int
	opener := func(ctx context.Context, lastEventID string) (io.ReadCloser, error) {
		opens++
		if opens == 1 {
			return io.NopCloser(strings.NewReader(
				"id: 1\nevent: log\ndata: {\"id\":\"1\",\"message\":\"first\"}\n\n",
			)), nil
		}
		// Replays event 1 again, then delivers the genuinely new event 2.
		return io.NopCloser(strings.NewReader(
			"id: 1\nevent: log\ndata: {\"id\":\"1\",\"message\":\"first\"}\n\n" +
				"id: 2\nevent: log\ndata: {\"id\":\"2\",\"message\":\"second\"}\n\n",
		)), nil
	}

	rs := NewReconnectingStream(context.Background(), opener)

	ev, err := rs.Next()
	if err != nil {
		t.Fatalf("Next (1): %v", err)
	}
	if ev.Message != "first" {
		t.Fatalf("first event = %+v, want message=first", ev)
	}

	ev, err = rs.Next()
	if err != nil {
		t.Fatalf("Next (2): %v", err)
	}
	if ev.Message != "second" {
		t.Errorf("expected the replayed id=1 event to be skipped and id=2 delivered, got %+v", ev)
	}
}

func TestHTTPClient_GetRunLogs(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/runs/run-123/logs" {
			t.Errorf("path = %q, want /api/runs/run-123/logs", r.URL.Path)
		}
		if r.URL.Query().Get("offset") != "10" || r.URL.Query().Get("limit") != "20" {
			t.Errorf("query = %q, want offset=10&limit=20", r.URL.RawQuery)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(RunLogsPayload{
			RunID:      "run-123",
			Events:     []LogEvent{{ID: "1", Message: "hello"}},
			TotalCount: 100,
			Offset:     10,
			Limit:      20,
		})
	}))
	defer ts.Close()

	client, err := NewHTTPClient(ts.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	payload, err := client.GetRunLogs(context.Background(), "run-123", 10, 20)
	if err != nil {
		t.Fatalf("GetRunLogs: %v", err)
	}
	if payload.RunID != "run-123" || payload.TotalCount != 100 || len(payload.Events) != 1 {
		t.Errorf("unexpected payload: %+v", payload)
	}
}

func TestHTTPClient_GetRunLogs_Error(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, `{"error":"run log not found"}`, http.StatusNotFound)
	}))
	defer ts.Close()

	client, err := NewHTTPClient(ts.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}

	_, err = client.GetRunLogs(context.Background(), "nonexistent", 0, 10)
	if err == nil {
		t.Fatal("expected error for 404 response")
	}
}

func TestHTTPClient_GetRunEvents(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/runs/run-123/events" {
			t.Errorf("path = %q, want /api/runs/run-123/events", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(RunEventsPayload{
			RunID: "run-123",
			Events: []ExecutorEvent{{
				TicketID: "TICK-123",
				Progress: ExecutorProgress{Activity: "testing", TestSummary: &TestSummary{Status: "pass", Passed: 4}},
			}},
		})
	}))
	defer ts.Close()

	c, err := NewHTTPClient(ts.URL)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}
	payload, err := c.GetRunEvents(context.Background(), "run-123")
	if err != nil {
		t.Fatalf("GetRunEvents: %v", err)
	}
	if payload.RunID != "run-123" || len(payload.Events) != 1 || payload.Events[0].Progress.TestSummary.Passed != 4 {
		t.Errorf("unexpected payload: %+v", payload)
	}
}

// TestNoPrivateTicketReferences guards against private LaneGate ticket IDs
// (e.g. "TICK-304") leaking into comments in the client package's own
// source files. Fixture/test data using ticket-shaped IDs (as in the tests
// above) is exempt; only the non-test source files are scanned.
func TestNoPrivateTicketReferences(t *testing.T) {
	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime.Caller failed")
	}
	dir := filepath.Dir(thisFile)
	ticketRef := regexp.MustCompile(`TICK-\d+`)
	for _, name := range []string{"types.go", "http.go", "fixtures.go"} {
		data, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			t.Fatalf("reading %s: %v", name, err)
		}
		if m := ticketRef.FindString(string(data)); m != "" {
			t.Errorf("%s contains private ticket reference %q", name, m)
		}
	}
}

// Compile-time assertions that both implementations satisfy the Client
// interface.
var (
	_ Client = (*FixtureClient)(nil)
	_ Client = (*HTTPClient)(nil)
)
