package client

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"strconv"
	"strings"
)

// SSEEvent is one parsed server-sent event frame: an optional id/type plus
// the joined data lines, matching the id:/event:/data: framing that
// lanegate.api._sse_event writes.
type SSEEvent struct {
	ID   string
	Type string
	Data string
}

// DecodeLogEvent decodes an SSEEvent's Data field (a JSON object per
// lanegate.api._sse_event) into a LogEvent. An event with no data decodes to
// the zero LogEvent.
func DecodeLogEvent(e SSEEvent) (LogEvent, error) {
	var ev LogEvent
	if strings.TrimSpace(e.Data) == "" {
		return ev, nil
	}
	err := json.Unmarshal([]byte(e.Data), &ev)
	return ev, err
}

// ParseSSE reads a complete text/event-stream body from r and returns every
// frame it contains, in order. It is used for static/fixture streams; for a
// live stream that must be read incrementally, use LogStream instead.
func ParseSSE(r io.Reader) ([]SSEEvent, error) {
	ls := NewLogStreamRaw(io.NopCloser(r))
	var events []SSEEvent
	for {
		ev, err := ls.nextRaw()
		if err != nil {
			if err == io.EOF {
				return events, nil
			}
			return events, err
		}
		events = append(events, ev)
	}
}

// LogStream reads a live text/event-stream body incrementally, decoding one
// SSE frame at a time as it arrives on the wire rather than waiting for the
// connection to close — required for a log tail that never closes while an
// orchestration run is active.
type LogStream struct {
	r      *bufio.Reader
	closer io.Closer
}

// NewLogStreamRaw wraps rc for incremental SSE frame reads without decoding
// frame data as a LogEvent (used by ParseSSE against static bodies).
func NewLogStreamRaw(rc io.ReadCloser) *LogStream {
	return &LogStream{r: bufio.NewReader(rc), closer: rc}
}

// NewLogStream wraps rc for incremental, decoded LogEvent reads.
func NewLogStream(rc io.ReadCloser) *LogStream {
	return NewLogStreamRaw(rc)
}

// Close releases the underlying reader.
func (s *LogStream) Close() error {
	if s.closer != nil {
		return s.closer.Close()
	}
	return nil
}

// nextRaw returns the next complete SSE frame without decoding its data as a
// LogEvent, or io.EOF once the stream ends with no frame in flight.
func (s *LogStream) nextRaw() (SSEEvent, error) {
	var cur SSEEvent
	var dataLines []string
	hasContent := false

	for {
		line, err := s.r.ReadString('\n')
		trimmed := strings.TrimRight(line, "\r\n")

		if trimmed != "" {
			switch {
			case strings.HasPrefix(trimmed, "id:"):
				cur.ID = strings.TrimSpace(strings.TrimPrefix(trimmed, "id:"))
				hasContent = true
			case strings.HasPrefix(trimmed, "event:"):
				cur.Type = strings.TrimSpace(strings.TrimPrefix(trimmed, "event:"))
				hasContent = true
			case strings.HasPrefix(trimmed, "data:"):
				dataLines = append(dataLines, strings.TrimPrefix(strings.TrimPrefix(trimmed, "data:"), " "))
				hasContent = true
			}
		} else if hasContent {
			cur.Data = strings.Join(dataLines, "\n")
			return cur, nil
		}

		if err != nil {
			if hasContent {
				// A frame reached EOF without its terminating blank line
				// (e.g. a truncated fixture file); still return what was
				// accumulated rather than dropping it silently.
				cur.Data = strings.Join(dataLines, "\n")
				return cur, nil
			}
			return SSEEvent{}, err
		}
	}
}

// Next returns the next complete SSE frame, decoded as a LogEvent.
func (s *LogStream) Next() (LogEvent, error) {
	ev, err := s.nextRaw()
	if err != nil {
		return LogEvent{}, err
	}
	return DecodeLogEvent(ev)
}

// StreamOpener opens (or reopens) the run-log SSE stream, given the last
// event id already consumed (used only for client-side dedup on reconnect —
// see ReconnectingStream.Next — since the server replays the log from the
// start on every new connection rather than honoring Last-Event-ID).
type StreamOpener func(ctx context.Context, lastEventID string) (io.ReadCloser, error)

// ReconnectingStream wraps a StreamOpener and transparently reconnects once
// per Next() call if the underlying connection drops, skipping any replayed
// events at or before the last event id already delivered so the caller
// never sees a duplicate log line after a reconnect.
type ReconnectingStream struct {
	ctx         context.Context
	open        StreamOpener
	current     *LogStream
	lastEventID string
}

// NewReconnectingStream creates a stream that lazily opens on the first
// Next() call.
func NewReconnectingStream(ctx context.Context, open StreamOpener) *ReconnectingStream {
	return &ReconnectingStream{ctx: ctx, open: open}
}

// Next blocks until the next new LogEvent is available. On a read error it
// makes one reconnect attempt (via the StreamOpener) before propagating the
// error; a successful reconnect transparently continues the caller's loop.
func (rs *ReconnectingStream) Next() (LogEvent, error) {
	reconnected := false
	for {
		if rs.current == nil {
			if err := rs.connect(); err != nil {
				return LogEvent{}, err
			}
		}

		ev, err := rs.current.Next()
		if err != nil {
			rs.current.Close()
			rs.current = nil
			if reconnected {
				return LogEvent{}, err
			}
			reconnected = true
			continue
		}

		if !isNewerEventID(ev.ID, rs.lastEventID) {
			// Already delivered before a reconnect replayed it from the top.
			continue
		}
		if ev.ID != "" {
			rs.lastEventID = ev.ID
		}
		return ev, nil
	}
}

// Close releases the current underlying connection, if any.
func (rs *ReconnectingStream) Close() error {
	if rs.current != nil {
		return rs.current.Close()
	}
	return nil
}

func (rs *ReconnectingStream) connect() error {
	rc, err := rs.open(rs.ctx, rs.lastEventID)
	if err != nil {
		return err
	}
	rs.current = NewLogStream(rc)
	return nil
}

// isNewerEventID reports whether id should be delivered given the last id
// already seen. Event ids are decimal line numbers (see
// lanegate.api._stream_log_events), so a numeric comparison is used when
// possible; any non-numeric id is treated as always-new (e.g. status events
// with no id).
func isNewerEventID(id, last string) bool {
	if last == "" || id == "" {
		return true
	}
	idNum, idErr := strconv.Atoi(id)
	lastNum, lastErr := strconv.Atoi(last)
	if idErr == nil && lastErr == nil {
		return idNum > lastNum
	}
	return id != last
}
