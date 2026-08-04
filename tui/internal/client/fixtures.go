package client

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// FixtureClient implements Client by loading fixtures from the filesystem.
type FixtureClient struct {
	fixtureDir string
	cache      map[string]interface{}
}

// NewFixtureClient creates a fixture-based client loading from fixtureDir.
func NewFixtureClient(fixtureDir string) (*FixtureClient, error) {
	if fixtureDir == "" {
		return nil, fmt.Errorf("fixture directory is required")
	}

	info, err := os.Stat(fixtureDir)
	if err != nil {
		return nil, fmt.Errorf("fixture directory: %w", err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("fixture path is not a directory")
	}

	return &FixtureClient{
		fixtureDir: fixtureDir,
		cache:      make(map[string]interface{}),
	}, nil
}

// GetBoard loads a board fixture
func (fc *FixtureClient) GetBoard(ctx context.Context) (*BoardPayload, error) {
	return fc.loadBoard(ctx, "board/board_basic.json")
}

// GetTickets loads a tickets fixture
func (fc *FixtureClient) GetTickets(ctx context.Context) (*TicketsPayload, error) {
	return fc.loadTickets(ctx, "board/tickets_flat.json")
}

// GetTicketDetail loads a ticket detail fixture
func (fc *FixtureClient) GetTicketDetail(ctx context.Context, ticketID string) (*TicketDetail, error) {
	// Try to load a fixture based on ticket ID or default to ready fixture
	path := filepath.Join("ticket_detail", ticketID+".json")
	if _, err := os.Stat(filepath.Join(fc.fixtureDir, path)); err != nil {
		path = "ticket_detail/ticket_ready.json"
	}
	return fc.loadTicketDetail(ctx, path)
}

// GetBlocked loads a blocked queue fixture
func (fc *FixtureClient) GetBlocked(ctx context.Context) (*BlockedPayload, error) {
	return fc.loadBlocked(ctx, "blocked/blocked_queue.json")
}

// GetDiff loads a diff fixture based on ticket ID, falling back to a small
// default diff when no ID-specific fixture exists (fixtures are named
// descriptively, not by ticket ID, matching GetTicketDetail's convention).
func (fc *FixtureClient) GetDiff(ctx context.Context, ticketID string) (*DiffPayload, error) {
	path := filepath.Join("diff", ticketID+".json")
	if _, err := os.Stat(filepath.Join(fc.fixtureDir, path)); err != nil {
		path = "diff/diff_small.json"
	}
	return fc.loadDiff(ctx, path)
}

// GetCurrentRun loads a run-state fixture
func (fc *FixtureClient) GetCurrentRun(ctx context.Context) (*RunPayload, error) {
	return fc.loadRun(ctx, "run/run_active.json")
}

// GetSettings loads a settings fixture
func (fc *FixtureClient) GetSettings(ctx context.Context) (*SettingsPayload, error) {
	return fc.loadSettings(ctx, "settings/settings_basic.json")
}

// GetPools loads a pools fixture
func (fc *FixtureClient) GetPools(ctx context.Context) (*PoolsPayload, error) {
	return fc.loadPools(ctx, "pools/pools_basic.json")
}

// UpdatePoolExecutors simulates a persisted reorder in fixture mode.
// Fixture mode has no real .lanegate.yml to write back to, so this only
// echoes the requested order back as the "saved" result — it does not
// mutate the on-disk fixture, and a subsequent GetPools call will still
// return the fixture's original order.
func (fc *FixtureClient) UpdatePoolExecutors(ctx context.Context, poolName string, executors []string) (*Pool, error) {
	payload, err := fc.GetPools(ctx)
	if err != nil {
		return nil, err
	}
	for _, p := range payload.Pools {
		if p.Name == poolName {
			return &Pool{
				Name:      poolName,
				Strategy:  p.Strategy,
				Executors: executors,
			}, nil
		}
	}
	return nil, fmt.Errorf("pool %q not found", poolName)
}

// OpenRunLogStream loads a static SSE fixture and returns it as a stream.
// lastEventID is ignored: fixture mode always replays the same file, and
// ReconnectingStream's dedup logic filters already-seen ids as usual.
func (fc *FixtureClient) OpenRunLogStream(ctx context.Context, lastEventID string) (io.ReadCloser, error) {
	data, err := fc.readFixture("run/events_basic.sse")
	if err != nil {
		return nil, err
	}
	return io.NopCloser(bytes.NewReader(data)), nil
}

// GetRunLogPage serves a page of the same events_basic.sse fixture used by
// OpenRunLogStream, decoded and sliced by offset/limit so fixture mode
// exercises the same history/pagination path as a live server.
func (fc *FixtureClient) GetRunLogPage(ctx context.Context, offset, limit int) (*LogPagePayload, error) {
	data, err := fc.readFixture("run/events_basic.sse")
	if err != nil {
		return nil, err
	}
	frames, err := ParseSSE(bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("parse events_basic.sse: %w", err)
	}

	var lines []string
	var runID string
	for _, f := range frames {
		ev, err := DecodeLogEvent(f)
		if err != nil || ev.Message == "" {
			continue
		}
		if runID == "" {
			runID = ev.RunID
		}
		lines = append(lines, ev.Message)
	}

	total := len(lines)
	if offset > total {
		offset = total
	}
	end := offset + limit
	if end > total {
		end = total
	}
	page := lines[offset:end]

	events := make([]LogEvent, len(page))
	for i, msg := range page {
		events[i] = LogEvent{
			ID:      fmt.Sprintf("%d", offset+i+1),
			Type:    "log",
			RunID:   runID,
			Message: msg,
		}
	}

	var nextOffset *int
	if n := offset + len(page); n < total {
		nextOffset = &n
	}

	return &LogPagePayload{
		RunID:      runID,
		Offset:     offset,
		Limit:      limit,
		TotalCount: total,
		NextOffset: nextOffset,
		Events:     events,
	}, nil
}

// GetRunHistory loads run history fixture
func (fc *FixtureClient) GetRunHistory(ctx context.Context) (*RunHistoryPayload, error) {
	path := "run/history.json"
	if _, err := os.Stat(filepath.Join(fc.fixtureDir, path)); err != nil {
		return &RunHistoryPayload{Runs: []RunSummaryPayload{}}, nil
	}
	data, err := fc.readFixture(path)
	if err != nil {
		return nil, err
	}
	var payload RunHistoryPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		var runs []RunSummaryPayload
		if err2 := json.Unmarshal(data, &runs); err2 == nil {
			return &RunHistoryPayload{Runs: runs}, nil
		}
		return nil, fmt.Errorf("decode run history fixture: %w", err)
	}
	if payload.Runs == nil {
		payload.Runs = []RunSummaryPayload{}
	}
	return &payload, nil
}

// GetRunSummary loads a single run summary fixture
func (fc *FixtureClient) GetRunSummary(ctx context.Context, runID string) (*RunSummaryPayload, error) {
	history, err := fc.GetRunHistory(ctx)
	if err != nil {
		return nil, err
	}
	for _, r := range history.Runs {
		if r.RunID == runID {
			return &r, nil
		}
	}
	if len(history.Runs) > 0 {
		return &history.Runs[0], nil
	}
	return nil, fmt.Errorf("run summary %q not found", runID)
}

// Private helpers

func (fc *FixtureClient) loadBoard(ctx context.Context, path string) (*BoardPayload, error) {
	data, err := fc.readFixture(path)
	if err != nil {
		return nil, err
	}

	var payload BoardPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode board fixture: %w", err)
	}

	if payload.Tickets == nil {
		payload.Tickets = make(map[string][]Ticket)
	}
	if payload.Pipeline == nil {
		payload.Pipeline = []PipelineEntry{}
	}

	return &payload, nil
}

func (fc *FixtureClient) loadTickets(ctx context.Context, path string) (*TicketsPayload, error) {
	data, err := fc.readFixture(path)
	if err != nil {
		return nil, err
	}

	var payload TicketsPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode tickets fixture: %w", err)
	}

	if payload.Tickets == nil {
		payload.Tickets = []Ticket{}
	}

	return &payload, nil
}

func (fc *FixtureClient) loadTicketDetail(ctx context.Context, path string) (*TicketDetail, error) {
	data, err := fc.readFixture(path)
	if err != nil {
		return nil, err
	}

	var detail TicketDetail
	if err := json.Unmarshal(data, &detail); err != nil {
		return nil, fmt.Errorf("decode ticket detail fixture: %w", err)
	}

	return &detail, nil
}

func (fc *FixtureClient) loadBlocked(ctx context.Context, path string) (*BlockedPayload, error) {
	data, err := fc.readFixture(path)
	if err != nil {
		return nil, err
	}

	var payload BlockedPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode blocked fixture: %w", err)
	}

	if payload.Blocked == nil {
		payload.Blocked = []BlockedTicket{}
	}

	return &payload, nil
}

func (fc *FixtureClient) loadDiff(ctx context.Context, path string) (*DiffPayload, error) {
	data, err := fc.readFixture(path)
	if err != nil {
		return nil, err
	}

	var payload DiffPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode diff fixture: %w", err)
	}
	if payload.Files == nil {
		payload.Files = []DiffFile{}
	}

	return &payload, nil
}

func (fc *FixtureClient) loadRun(ctx context.Context, path string) (*RunPayload, error) {
	data, err := fc.readFixture(path)
	if err != nil {
		return nil, err
	}

	var payload RunPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode run fixture: %w", err)
	}
	if payload.Workers == nil {
		payload.Workers = []RunWorker{}
	}
	if payload.Tickets == nil {
		payload.Tickets = []string{}
	}

	return &payload, nil
}

func (fc *FixtureClient) loadSettings(ctx context.Context, path string) (*SettingsPayload, error) {
	data, err := fc.readFixture(path)
	if err != nil {
		return nil, err
	}

	var payload SettingsPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode settings fixture: %w", err)
	}
	if payload.Environments == nil {
		payload.Environments = []SettingsEnvironment{}
	}

	return &payload, nil
}

func (fc *FixtureClient) loadPools(ctx context.Context, path string) (*PoolsPayload, error) {
	data, err := fc.readFixture(path)
	if err != nil {
		return nil, err
	}

	var payload PoolsPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode pools fixture: %w", err)
	}
	if payload.Pools == nil {
		payload.Pools = []Pool{}
	}

	return &payload, nil
}

// GetRunLogs loads the shared raw-audit fixture and slices it to the
// requested [offset, offset+limit) window, exercising the same paginated
// audit path fixture mode uses for the Run screen's explicit Raw Audit Log
// mode as HTTP mode does against the live API.
func (fc *FixtureClient) GetRunLogs(ctx context.Context, runID string, offset, limit int) (*RunLogsPayload, error) {
	data, err := fc.readFixture("run/raw_audit_page.json")
	if err != nil {
		return nil, err
	}

	var full RunLogsPayload
	if err := json.Unmarshal(data, &full); err != nil {
		return nil, fmt.Errorf("decode run logs fixture: %w", err)
	}
	all := full.Events
	if all == nil {
		all = []LogEvent{}
	}

	total := len(all)
	if offset < 0 {
		offset = 0
	}
	if offset > total {
		offset = total
	}
	if limit <= 0 {
		limit = total - offset
	}
	end := offset + limit
	if end > total {
		end = total
	}

	var nextOffset *int
	if end < total {
		n := end
		nextOffset = &n
	}

	return &RunLogsPayload{
		RunID:      runID,
		Events:     all[offset:end],
		TotalCount: total,
		Offset:     offset,
		Limit:      limit,
		NextOffset: nextOffset,
	}, nil
}

// GetRunEvents loads a TICK-307 safe structured-event fixture: the live
// fixture for "" / "current", the historical fixture for any other run ID.
func (fc *FixtureClient) GetRunEvents(ctx context.Context, runID string) (*RunEventsPayload, error) {
	path := "run/executor_events_live.json"
	if runID != "" && runID != "current" {
		path = "run/executor_events_historical.json"
	}

	data, err := fc.readFixture(path)
	if err != nil {
		return nil, err
	}

	var payload RunEventsPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode run events fixture: %w", err)
	}
	if payload.Events == nil {
		payload.Events = []ExecutorEvent{}
	}

	return &payload, nil
}

func (fc *FixtureClient) readFixture(path string) ([]byte, error) {
	fullPath := filepath.Join(fc.fixtureDir, path)
	data, err := os.ReadFile(fullPath)
	if err != nil {
		return nil, fmt.Errorf("read fixture %s: %w", path, err)
	}
	return data, nil
}
