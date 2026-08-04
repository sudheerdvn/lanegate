package client

import (
	"context"
	"io"
)

// Client is the interface for fetching LaneGate API data.
// Implementations can be HTTP-based or fixture-based.
type Client interface {
	// GetBoard fetches the current board state
	GetBoard(ctx context.Context) (*BoardPayload, error)

	// GetTickets fetches all visible tickets as a flat list
	GetTickets(ctx context.Context) (*TicketsPayload, error)

	// GetTicketDetail fetches full details for a single ticket
	GetTicketDetail(ctx context.Context, ticketID string) (*TicketDetail, error)

	// GetBlocked fetches the blocked/review queue
	GetBlocked(ctx context.Context) (*BlockedPayload, error)

	// GetDiff fetches the structured diff for a ticket's branch vs main
	GetDiff(ctx context.Context, ticketID string) (*DiffPayload, error)

	// GetCurrentRun fetches current orchestration-run state
	GetCurrentRun(ctx context.Context) (*RunPayload, error)

	// GetSettings fetches the sanitized resolved config/API metadata
	GetSettings(ctx context.Context) (*SettingsPayload, error)

	// GetPools fetches pools.<name>.executors lists (in preference order)
	// plus persisted rotation/dispatch state (TICK-269).
	GetPools(ctx context.Context) (*PoolsPayload, error)

	// UpdatePoolExecutors persists a reordered executors list for one pool
	// and returns the pool's updated {name, strategy, executors}. executors
	// must be a reordering of the pool's current executor set — adding or
	// removing instances through this call is rejected server-side.
	UpdatePoolExecutors(ctx context.Context, poolName string, executors []string) (*Pool, error)

	// OpenRunLogStream opens the run-log SSE stream. lastEventID is passed
	// through to a StreamOpener on reconnect (see ReconnectingStream); it is
	// otherwise ignored by implementations that always replay from the top.
	OpenRunLogStream(ctx context.Context, lastEventID string) (io.ReadCloser, error)

	// GetRunLogPage fetches a bounded, offset-addressed page of Activity from
	// GET /api/runs/current/logs — the authoritative on-disk log, used to
	// reach history older than the live SSE tail's in-memory cap.
	GetRunLogPage(ctx context.Context, offset, limit int) (*LogPagePayload, error)

	// GetRunHistory fetches run summaries from GET /api/runs
	GetRunHistory(ctx context.Context) (*RunHistoryPayload, error)

	// GetRunSummary fetches a single run summary from GET /api/runs/{id}
	GetRunSummary(ctx context.Context, runID string) (*RunSummaryPayload, error)

	// GetRunLogs fetches paginated raw audit log events for a run from
	// GET /api/runs/{id}/logs. Reserved for the Run screen's explicit Raw
	// Audit Log mode.
	GetRunLogs(ctx context.Context, runID string, offset, limit int) (*RunLogsPayload, error)

	// GetRunEvents fetches TICK-307 safe, bounded structured executor-progress
	// events for a run from GET /api/runs/{id}/events. This is the source for
	// the Run screen's default Activity pane.
	GetRunEvents(ctx context.Context, runID string) (*RunEventsPayload, error)
}
