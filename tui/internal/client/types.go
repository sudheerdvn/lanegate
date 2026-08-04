package client

// BoardPayload represents the response from GET /api/board
type BoardPayload struct {
	Tickets  map[string][]Ticket `json:"tickets"`
	Pipeline []PipelineEntry     `json:"pipeline"`
}

// Ticket represents a single ticket in the board
type Ticket struct {
	ID                string   `json:"id"`
	Title             string   `json:"title"`
	Status            string   `json:"status"`
	Priority          int      `json:"priority"`
	Milestone         string   `json:"milestone"`
	Touches           []string `json:"touches"`
	DependsOn         []string `json:"depends_on"`
	Branch            string   `json:"branch"`
	Worktree          string   `json:"worktree"`
	ImplementExecutor string   `json:"implement_executor"`
	ReviewExecutor    string   `json:"review_executor"`
	ExecutionMode     string   `json:"execution_mode"`
	ReviewVerdict     string   `json:"review_verdict"`
}

// PipelineEntry represents a deployment pipeline entry
type PipelineEntry struct {
	Env          string   `json:"env"`
	Base         string   `json:"base"`
	Head         string   `json:"head"`
	Trigger      string   `json:"trigger"`
	PendingCount int      `json:"pending_count"`
	Commits      []string `json:"commits"`
}

// TicketsPayload represents the response from GET /api/tickets
type TicketsPayload struct {
	Tickets []Ticket `json:"tickets"`
}

// TicketDetail represents the response from GET /api/tickets/{id}
type TicketDetail struct {
	ID              string           `json:"id"`
	Title           string           `json:"title"`
	Status          string           `json:"status"`
	Priority        int              `json:"priority"`
	Milestone       string           `json:"milestone"`
	Touches         []string         `json:"touches"`
	DependsOn       []string         `json:"depends_on"`
	Branch          string           `json:"branch"`
	Worktree        string           `json:"worktree"`
	Body            string           `json:"body"`
	CloseCriteria   string           `json:"close_criteria"`
	ReviewVerdict   string           `json:"review_verdict"`
	ReviewSummary   string           `json:"review_summary"`
	ReviewFindings  []string         `json:"review_findings"`
	LifecycleEvents []LifecycleEvent `json:"lifecycle_events"`
}

// LifecycleEvent is one durable ticket transition or review/merge outcome.
type LifecycleEvent struct {
	At         string `json:"at"`
	Event      string `json:"event"`
	FromStatus string `json:"from_status"`
	ToStatus   string `json:"to_status"`
	Summary    string `json:"summary"`
}

// BlockedPayload represents the response from GET /api/blocked
type BlockedPayload struct {
	Blocked []BlockedTicket `json:"blocked"`
}

// BlockedTicket represents a single blocked ticket
type BlockedTicket struct {
	ID        string   `json:"id"`
	Title     string   `json:"title"`
	Branch    string   `json:"branch"`
	DiffCmd   string   `json:"diff_cmd"`
	Priority  int      `json:"priority"`
	Milestone string   `json:"milestone"`
	Findings  []string `json:"findings"`
}

// ErrorPayload represents a structured error response
type ErrorPayload struct {
	Error string `json:"error"`
}

// DiffPayload represents the response from GET /api/diff/{id} (see
// lanegate.ticket.get_ticket_diff). Patches are pre-truncated server-side per
// file; Truncated/MaxPatchChars carry that so the renderer never needs to
// re-bound an already-bounded string.
type DiffPayload struct {
	ID            string     `json:"id"`
	TicketID      string     `json:"ticket_id"`
	Branch        string     `json:"branch"`
	Base          string     `json:"base"`
	Stat          string     `json:"stat"`
	Files         []DiffFile `json:"files"`
	Diff          string     `json:"diff"`
	Truncated     bool       `json:"truncated"`
	MaxPatchChars int        `json:"max_patch_chars"`
	Error         string     `json:"error"`
}

// DiffFile represents one changed file within a DiffPayload.
type DiffFile struct {
	Path      string `json:"path"`
	OldPath   string `json:"old_path"`
	Status    string `json:"status"`
	Patch     string `json:"patch"`
	Truncated bool   `json:"truncated"`
	Error     string `json:"error"`
}

// ResumeWatchStatus represents the state of the resume-watch daemon as
// derived from its PID file and history JSONL (see lanegate.resume_watch).
// The field is absent (null) when no daemon is running.
type ResumeWatchStatus struct {
	// Phase is "waiting", "retrying", or "gave_up".
	Phase string `json:"phase"`
	// ElapsedTime is seconds since the rate-limit hibernation started.
	ElapsedTime float64 `json:"elapsed_time"`
	// NextRetryETA is an ISO-8601 timestamp for the next retry attempt, or
	// null when not computable without access to the daemon's backoff config.
	NextRetryETA *string `json:"next_retry_eta"`
}

// RunPayload represents the response from GET /api/runs/current.
type RunPayload struct {
	RunID             string             `json:"run_id"`
	Status            string             `json:"status"`
	StartedAtISO      string             `json:"started_at_iso"`
	OrchestratorPID   int                `json:"orchestrator_pid"`
	ProcessAlive      bool               `json:"process_alive"`
	StopRequested     bool               `json:"stop_requested"`
	Tickets           []string           `json:"tickets"`
	Workers           []RunWorker        `json:"workers"`
	LastEventID       int                `json:"last_event_id"`
	Orchestration     *Orchestration     `json:"orchestration"`
	ResumeWatchStatus *ResumeWatchStatus `json:"resume_watch_status"`
}

// RunWorker represents one active executor within a RunPayload. Today the
// Python core tracks at most one active executor (see TICK-089 gap noted in
// TICK-157), so Workers has 0 or 1 entries — the shape is a list so the
// screen renders correctly once multi-worker aggregation lands.
type RunWorker struct {
	TicketID            string `json:"ticket_id"`
	ExecutorPID         int    `json:"executor_pid"`
	State               string `json:"state"`
	ReconciliationState string `json:"reconciliation_state"`
	ResolvedDriver      string `json:"resolved_driver"`
	ResolvedExecutor    string `json:"resolved_executor"`
	ResolvedModel       string `json:"resolved_model"`
}

// Orchestration mirrors lanegate.orchestrate.get_orchestration_status().
type Orchestration struct {
	Active              bool           `json:"active"`
	State               string         `json:"state"`
	ReconciliationState string         `json:"reconciliation_state"`
	ExecutorPID         int            `json:"executor_pid"`
	TicketID            string         `json:"ticket_id"`
	HeartbeatCount      int            `json:"heartbeat_count"`
	ResolvedDriver      string         `json:"resolved_driver"`
	ResolvedExecutor    string         `json:"resolved_executor"`
	ResolvedModel       string         `json:"resolved_model"`
	LastCooldown        *CooldownEvent `json:"last_cooldown"`
}

// CooldownEvent identifies which pool executor instance most recently hit a
// rate-limit cooldown, so the Run screen can say "claude-a" / "codex" / etc.
// instead of just "some executor is rate-limited" (which is all
// ResumeWatchStatus's phase/elapsed_time alone can say).
type CooldownEvent struct {
	Instance string `json:"instance"`
	Reason   string `json:"reason"`
	Ts       string `json:"ts"`
}

// SettingsPayload represents the response from GET /api/config (also served
// at /api/settings). It is a sanitized, read-only view — secret-shaped
// fields are already redacted server-side before this is ever unmarshaled.
type SettingsPayload struct {
	RepoRoot            string                 `json:"repo_root"`
	TicketPrefix        string                 `json:"ticket_prefix"`
	TicketsDir          string                 `json:"tickets_dir"`
	WorktreesDir        string                 `json:"worktrees_dir"`
	Executor            string                 `json:"executor"`
	ExecutorSteps       map[string]interface{} `json:"executor_steps"`
	Executors           map[string]interface{} `json:"executors"`
	Models              map[string]interface{} `json:"models"`
	MaxParallel         int                    `json:"max_parallel"`
	DefaultMilestone    string                 `json:"default_milestone"`
	OnRateLimit         string                 `json:"on_rate_limit"`
	GithubPR            bool                   `json:"github_pr"`
	CommitStatusChanges bool                   `json:"commit_status_changes"`
	Environments        []SettingsEnvironment  `json:"environments"`
	API                 SettingsAPIMeta        `json:"api"`
}

// SettingsEnvironment is one delivery-axis environment entry within
// SettingsPayload.Environments.
type SettingsEnvironment struct {
	Name    string `json:"name"`
	Branch  string `json:"branch"`
	From    string `json:"from"`
	Trigger string `json:"trigger"`
	Sync    string `json:"sync"`
}

// SettingsAPIMeta describes the API server the TUI is currently talking to.
type SettingsAPIMeta struct {
	Host string `json:"host"`
	Port int    `json:"port"`
}

// PoolsPayload represents the response from GET /api/pools (TICK-269): the
// executors in each `pools.<name>` entry in their configured preference
// order, plus the rotation/dispatch state orchestrate persists per pool
// (TICK-268) across separate runs.
type PoolsPayload struct {
	Pools []Pool `json:"pools"`
}

// Pool is one `pools.<name>` entry. Executors is preference order: for
// least-loaded it's the tie-break order, for round-robin it's the starting
// rotation order — reordering it via PUT /api/pools/{name}/executors
// changes both.
type Pool struct {
	Name           string         `json:"name"`
	Strategy       string         `json:"strategy"`
	Executors      []string       `json:"executors"`
	DispatchCounts map[string]int `json:"dispatch_counts"`
	RRIndex        int            `json:"rr_index"`
	Default        bool           `json:"default"`
}

// LogEvent is one decoded SSE event from GET /api/runs/current/logs/stream
// (see lanegate.api._sse_event / _stream_log_events).
type LogEvent struct {
	ID        string                 `json:"id"`
	Type      string                 `json:"type"`
	Timestamp string                 `json:"timestamp"`
	RunID     string                 `json:"run_id"`
	TicketID  string                 `json:"ticket_id"`
	Message   string                 `json:"message"`
	Data      map[string]interface{} `json:"data"`
}

// LogPagePayload represents the response from GET /api/runs/current/logs —
// a bounded, offset-addressed page of Activity lines from the current run's
// authoritative on-disk log, distinct from the live SSE tail. NextOffset is
// nil once the requested page reaches the end of the file.
type LogPagePayload struct {
	RunID      string     `json:"run_id"`
	Offset     int        `json:"offset"`
	Limit      int        `json:"limit"`
	TotalCount int        `json:"total_count"`
	NextOffset *int       `json:"next_offset"`
	Events     []LogEvent `json:"events"`
}

// RunSummaryPayload represents one RunSummary from the Python API
// (lanegate.orchestrate.run_summary.RunSummary).
type RunSummaryPayload struct {
	RunID        string          `json:"run_id"`
	Timestamp    string          `json:"timestamp"`
	Reason       string          `json:"reason"`
	BatchTickets []TicketOutcome `json:"batch_tickets"`
}

// TicketOutcome represents one dispatched ticket's outcome within a RunSummaryPayload.
type TicketOutcome struct {
	TicketID        string  `json:"ticket_id"`
	Executor        string  `json:"executor"`
	Outcome         string  `json:"outcome"`
	DurationSeconds float64 `json:"duration_seconds"`
	FailureReason   *string `json:"failure_reason,omitempty"`
	ReviewReason    *string `json:"review_reason,omitempty"`
}

// RunHistoryPayload represents the response from GET /api/runs (list of RunSummaryPayload).
type RunHistoryPayload struct {
	Runs []RunSummaryPayload `json:"runs"`
}

// RunLogsPayload represents the response from GET /api/runs/{id}/logs. This
// is the raw, paginated audit trail (transcript/protocol-shaped messages)
// and is only ever rendered behind the Run screen's explicit Raw Audit Log
// mode — never the default Activity pane.
type RunLogsPayload struct {
	RunID      string     `json:"run_id"`
	Events     []LogEvent `json:"events"`
	TotalCount int        `json:"total_count"`
	Offset     int        `json:"offset"`
	Limit      int        `json:"limit"`
	NextOffset *int       `json:"next_offset"`
}
// RunEventsPayload represents the response from GET /api/runs/{id}/events
// (TICK-307): a bounded feed of normalized, safe executor-progress records
// for a run. This is the only run activity source the Run screen's default
// Activity pane may render — it never carries raw executor stdout,
// stream-JSON protocol lines, prompts, full shell commands, source content,
// reasoning, or secrets. See lanegate.orchestrate.run_report.read_executor_events
// and lanegate.executor_events.ExecutorEvent.
type RunEventsPayload struct {
	RunID  string          `json:"run_id"`
	Events []ExecutorEvent `json:"events"`
}

// ExecutorEvent is one safe executor-progress record within a
// RunEventsPayload.
type ExecutorEvent struct {
	Ts       string           `json:"ts"`
	Event    string           `json:"event"`
	TicketID string           `json:"ticket_id"`
	Progress ExecutorProgress `json:"progress"`
}

// ExecutorProgress is the bounded progress payload nested in ExecutorEvent
// (mirrors lanegate.executor_events.ExecutorEvent.to_dict). Path is already
// repo-relative and length-bounded server-side; strings are redacted and
// length-bounded server-side as well.
type ExecutorProgress struct {
	Phase         string         `json:"phase"`
	Activity      string         `json:"activity"`
	Ts            string         `json:"ts"`
	ActivityAge   float64        `json:"activity_age"`
	Executor      string         `json:"executor"`
	Model         string         `json:"model"`
	ToolCategory  string         `json:"tool_category"`
	Path          string         `json:"path"`
	TestSummary   *TestSummary   `json:"test_summary"`
	ProviderUsage *ProviderUsage `json:"provider_usage"`
}

// TestSummary is a concise test-run outcome nested in ExecutorProgress.
type TestSummary struct {
	Category string `json:"category"`
	Status   string `json:"status"`
	Passed   int    `json:"passed"`
	Failed   int    `json:"failed"`
}

// ProviderUsage is bounded token/cost usage nested in ExecutorProgress.
type ProviderUsage struct {
	InputTokens         float64 `json:"input_tokens"`
	OutputTokens        float64 `json:"output_tokens"`
	CacheReadTokens     float64 `json:"cache_read_tokens"`
	CacheCreationTokens float64 `json:"cache_creation_tokens"`
	CostUSD             float64 `json:"cost_usd"`
}
