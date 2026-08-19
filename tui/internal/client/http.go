package client

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// HTTPClient implements Client over a remote HTTP API.
type HTTPClient struct {
	baseURL      string
	client       *http.Client
	streamClient *http.Client
}

// NewHTTPClient creates a new HTTP-based client.
func NewHTTPClient(baseURL string) (*HTTPClient, error) {
	// Normalize URL
	baseURL = strings.TrimRight(baseURL, "/")

	// Validate scheme
	if !strings.HasPrefix(baseURL, "http://") && !strings.HasPrefix(baseURL, "https://") {
		return nil, fmt.Errorf("URL must start with http:// or https://")
	}

	return &HTTPClient{
		baseURL: baseURL,
		client: &http.Client{
			Timeout: 10 * time.Second,
		},
		// The run-log SSE endpoint is a long-lived connection by design (it
		// stays open while an orchestration run is active), so it must not
		// share the 10s request timeout used for ordinary JSON GETs — that
		// would sever a healthy stream every 10 seconds. Cancellation is via
		// the caller's context instead.
		streamClient: &http.Client{},
	}, nil
}

// GetBoard fetches the board state from GET /api/board
func (c *HTTPClient) GetBoard(ctx context.Context) (*BoardPayload, error) {
	url := c.baseURL + "/api/board"
	data, err := c.get(ctx, url)
	if err != nil {
		return nil, err
	}

	var payload BoardPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode board payload: %w", err)
	}

	if payload.Tickets == nil {
		payload.Tickets = make(map[string][]Ticket)
	}
	if payload.Pipeline == nil {
		payload.Pipeline = []PipelineEntry{}
	}

	return &payload, nil
}

// GetTickets fetches all tickets from GET /api/tickets
func (c *HTTPClient) GetTickets(ctx context.Context) (*TicketsPayload, error) {
	url := c.baseURL + "/api/tickets"
	data, err := c.get(ctx, url)
	if err != nil {
		return nil, err
	}

	var payload TicketsPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode tickets payload: %w", err)
	}

	if payload.Tickets == nil {
		payload.Tickets = []Ticket{}
	}

	return &payload, nil
}

// GetTicketDetail fetches a single ticket from GET /api/tickets/{id}
func (c *HTTPClient) GetTicketDetail(ctx context.Context, ticketID string) (*TicketDetail, error) {
	if ticketID == "" {
		return nil, fmt.Errorf("ticket_id is required")
	}

	url := c.baseURL + "/api/tickets/" + ticketID
	data, err := c.get(ctx, url)
	if err != nil {
		return nil, err
	}

	// Check for error response
	var errPayload ErrorPayload
	if err := json.Unmarshal(data, &errPayload); err == nil && errPayload.Error != "" {
		return nil, fmt.Errorf("%s", errPayload.Error)
	}

	var detail TicketDetail
	if err := json.Unmarshal(data, &detail); err != nil {
		return nil, fmt.Errorf("decode ticket detail: %w", err)
	}

	return &detail, nil
}

// GetBlocked fetches the blocked queue from GET /api/blocked
func (c *HTTPClient) GetBlocked(ctx context.Context) (*BlockedPayload, error) {
	url := c.baseURL + "/api/blocked"
	data, err := c.get(ctx, url)
	if err != nil {
		return nil, err
	}

	var payload BlockedPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode blocked payload: %w", err)
	}

	if payload.Blocked == nil {
		payload.Blocked = []BlockedTicket{}
	}

	return &payload, nil
}

// GetDiff fetches a ticket's diff from GET /api/diff/{id}
func (c *HTTPClient) GetDiff(ctx context.Context, ticketID string) (*DiffPayload, error) {
	if ticketID == "" {
		return nil, fmt.Errorf("ticket_id is required")
	}

	url := c.baseURL + "/api/diff/" + ticketID
	data, err := c.get(ctx, url)
	if err != nil {
		return nil, err
	}

	var payload DiffPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode diff payload: %w", err)
	}
	if payload.Files == nil {
		payload.Files = []DiffFile{}
	}

	return &payload, nil
}

// GetCurrentRun fetches orchestration-run state from GET /api/runs/current
func (c *HTTPClient) GetCurrentRun(ctx context.Context) (*RunPayload, error) {
	url := c.baseURL + "/api/runs/current"
	data, err := c.get(ctx, url)
	if err != nil {
		return nil, err
	}

	var payload RunPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode run payload: %w", err)
	}
	if payload.Workers == nil {
		payload.Workers = []RunWorker{}
	}
	if payload.Tickets == nil {
		payload.Tickets = []string{}
	}

	return &payload, nil
}

// GetSettings fetches sanitized resolved config from GET /api/config
func (c *HTTPClient) GetSettings(ctx context.Context) (*SettingsPayload, error) {
	url := c.baseURL + "/api/config"
	data, err := c.get(ctx, url)
	if err != nil {
		return nil, err
	}

	var payload SettingsPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode settings payload: %w", err)
	}
	if payload.Environments == nil {
		payload.Environments = []SettingsEnvironment{}
	}

	return &payload, nil
}

// GetPools fetches pool executor order + dispatch state from GET /api/pools
func (c *HTTPClient) GetPools(ctx context.Context) (*PoolsPayload, error) {
	url := c.baseURL + "/api/pools"
	data, err := c.get(ctx, url)
	if err != nil {
		return nil, err
	}

	var payload PoolsPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode pools payload: %w", err)
	}
	if payload.Pools == nil {
		payload.Pools = []Pool{}
	}

	return &payload, nil
}

// UpdatePoolExecutors persists a reordered executors list via
// PUT /api/pools/{name}/executors.
func (c *HTTPClient) UpdatePoolExecutors(ctx context.Context, poolName string, executors []string) (*Pool, error) {
	url := c.baseURL + "/api/pools/" + poolName + "/executors"
	data, err := c.putJSON(ctx, url, map[string]interface{}{"executors": executors})
	if err != nil {
		return nil, err
	}

	var pool Pool
	if err := json.Unmarshal(data, &pool); err != nil {
		return nil, fmt.Errorf("decode pool payload: %w", err)
	}

	return &pool, nil
}

// OpenRunLogStream opens GET /api/runs/current/logs/stream and returns the
// live response body for incremental SSE reads. lastEventID is accepted for
// interface parity with StreamOpener but is not sent to the server: the
// Python endpoint always replays the log from the start on a fresh
// connection, so dedup on reconnect happens client-side in
// ReconnectingStream instead.
func (c *HTTPClient) OpenRunLogStream(ctx context.Context, lastEventID string) (io.ReadCloser, error) {
	url := c.baseURL + "/api/runs/current/logs/stream"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Accept", "text/event-stream")

	resp, err := c.streamClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("fetch %s: %w", url, err)
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(body))
	}

	return resp.Body, nil
}

// GetRunLogPage fetches a bounded Activity page from
// GET /api/runs/current/logs?offset=&limit=.
func (c *HTTPClient) GetRunLogPage(ctx context.Context, offset, limit int) (*LogPagePayload, error) {
	url := fmt.Sprintf("%s/api/runs/current/logs?offset=%d&limit=%d", c.baseURL, offset, limit)
	data, err := c.get(ctx, url)
	if err != nil {
		return nil, err
	}

	var payload LogPagePayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode log page payload: %w", err)
	}
	if payload.Events == nil {
		payload.Events = []LogEvent{}
	}

	return &payload, nil
}

// GetRunHistory fetches run summaries from GET /api/runs
func (c *HTTPClient) GetRunHistory(ctx context.Context) (*RunHistoryPayload, error) {
	url := c.baseURL + "/api/runs"
	data, err := c.get(ctx, url)
	if err != nil {
		return nil, err
	}

	var payload RunHistoryPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		var runs []RunSummaryPayload
		if err2 := json.Unmarshal(data, &runs); err2 == nil {
			return &RunHistoryPayload{Runs: runs}, nil
		}
		return nil, fmt.Errorf("decode run history payload: %w", err)
	}

	if payload.Runs == nil {
		payload.Runs = []RunSummaryPayload{}
	}

	return &payload, nil
}

// GetRunSummary fetches a single run summary from GET /api/runs/{id}
func (c *HTTPClient) GetRunSummary(ctx context.Context, runID string) (*RunSummaryPayload, error) {
	if runID == "" {
		return nil, fmt.Errorf("run_id is required")
	}

	url := c.baseURL + "/api/runs/" + runID
	data, err := c.get(ctx, url)
	if err != nil {
		return nil, err
	}

	var payload RunSummaryPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode run summary payload: %w", err)
	}

	return &payload, nil
}

// GetRunLogs fetches paginated log events for runID from GET /api/runs/{id}/logs
func (c *HTTPClient) GetRunLogs(ctx context.Context, runID string, offset, limit int) (*RunLogsPayload, error) {
	if runID == "" {
		runID = "current"
	}
	url := fmt.Sprintf("%s/api/runs/%s/logs?offset=%d&limit=%d", c.baseURL, runID, offset, limit)
	data, err := c.get(ctx, url)
	if err != nil {
		return nil, err
	}

	var payload RunLogsPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode run logs payload: %w", err)
	}
	if payload.Events == nil {
		payload.Events = []LogEvent{}
	}

	return &payload, nil
}

// GetRunEvents fetches safe structured executor-progress events for
// runID from GET /api/runs/{id}/events.
func (c *HTTPClient) GetRunEvents(ctx context.Context, runID string) (*RunEventsPayload, error) {
	if runID == "" {
		runID = "current"
	}
	url := c.baseURL + "/api/runs/" + runID + "/events"
	data, err := c.get(ctx, url)
	if err != nil {
		return nil, err
	}

	var payload RunEventsPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("decode run events payload: %w", err)
	}
	if payload.Events == nil {
		payload.Events = []ExecutorEvent{}
	}

	return &payload, nil
}

// get is a helper that fetches and returns raw JSON data
func (c *HTTPClient) get(ctx context.Context, url string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}

	req.Header.Set("Accept", "application/json")

	resp, err := c.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("fetch %s: %w", url, err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}

	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		var errPayload ErrorPayload
		if err := json.Unmarshal(body, &errPayload); err == nil && errPayload.Error != "" {
			return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, errPayload.Error)
		}
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(body))
	}

	return body, nil
}

// putJSON is a helper that PUTs a JSON-encoded body and returns the raw
// JSON response.
func (c *HTTPClient) putJSON(ctx context.Context, url string, payload interface{}) ([]byte, error) {
	buf, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("encode request body: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPut, url, bytes.NewReader(buf))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	resp, err := c.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("fetch %s: %w", url, err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}

	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		var errPayload ErrorPayload
		if err := json.Unmarshal(body, &errPayload); err == nil && errPayload.Error != "" {
			return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, errPayload.Error)
		}
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(body))
	}

	return body, nil
}
