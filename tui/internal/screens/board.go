package screens

import (
	"fmt"
	"sort"
	"strings"

	"lanegate/tui/internal/client"
	"lanegate/tui/internal/ui"
)

// BoardModel represents the board screen state (MVP placeholder)
type BoardModel struct {
	data          *client.BoardPayload
	selectedIndex int
}

// NewBoardModel creates a new board model
func NewBoardModel() *BoardModel {
	return &BoardModel{
		data: &client.BoardPayload{},
	}
}

// SetData updates the board data
func (bm *BoardModel) SetData(data *client.BoardPayload) {
	bm.data = data
	bm.clampSelection()
}

// GetData returns the board data
func (bm *BoardModel) GetData() *client.BoardPayload {
	return bm.data
}

// MoveSelection moves the active board row by delta, clamped to the current
// flattened board ticket list.
func (bm *BoardModel) MoveSelection(delta int) bool {
	tickets := bm.orderedTickets()
	if len(tickets) == 0 {
		bm.selectedIndex = 0
		return false
	}
	old := bm.selectedIndex
	bm.selectedIndex += delta
	bm.clampSelection()
	return bm.selectedIndex != old
}

// SelectTicket moves the active row to id when it exists on the board.
func (bm *BoardModel) SelectTicket(id string) bool {
	if id == "" {
		return false
	}
	for i, t := range bm.orderedTickets() {
		if t.ID == id {
			bm.selectedIndex = i
			return true
		}
	}
	return false
}

// SelectedTicketID returns the active board ticket id, or "" when empty.
func (bm *BoardModel) SelectedTicketID() string {
	tickets := bm.orderedTickets()
	if len(tickets) == 0 {
		return ""
	}
	bm.clampSelection()
	return tickets[bm.selectedIndex].ID
}

// SelectedIndex returns the active flattened board row.
func (bm *BoardModel) SelectedIndex() int {
	bm.clampSelection()
	return bm.selectedIndex
}

// boardStatusOrder defines the canonical, deterministic order in which
// status columns are rendered on the Board screen. Statuses not present in
// this list (unexpected/future statuses) are appended alphabetically so
// rendering never depends on Go's randomized map iteration order.
var boardStatusOrder = []string{
	"open", "in_progress", "code_complete", "in_review",
	"changes_requested", "blocked", "merged", "backlog",
}

// Render renders the board screen as plain text sized to width. Each status
// column is rendered as a labeled table of its tickets; empty boards render
// a single placeholder line.
func (bm *BoardModel) Render(width int) string {
	if bm.data == nil || len(bm.data.Tickets) == 0 {
		return "No tickets to display."
	}

	var sections []string
	rowIndex := 0
	for _, status := range orderedStatuses(bm.data.Tickets) {
		tickets := bm.data.Tickets[status]
		if len(tickets) == 0 {
			continue
		}
		sections = append(sections, bm.renderTicketGroup(status, tickets, width, &rowIndex))
	}

	if len(sections) == 0 {
		return "No tickets to display."
	}

	return strings.Join(sections, "\n\n")
}

// orderedStatuses returns the keys of byStatus in canonical board order,
// with any unrecognized statuses appended alphabetically.
func orderedStatuses(byStatus map[string][]client.Ticket) []string {
	seen := make(map[string]bool, len(byStatus))
	ordered := make([]string, 0, len(byStatus))
	for _, s := range boardStatusOrder {
		if _, ok := byStatus[s]; ok {
			ordered = append(ordered, s)
			seen[s] = true
		}
	}

	var extra []string
	for s := range byStatus {
		if !seen[s] {
			extra = append(extra, s)
		}
	}
	sort.Strings(extra)

	return append(ordered, extra...)
}

func (bm *BoardModel) renderTicketGroup(status string, tickets []client.Ticket, width int, rowIndex *int) string {
	header := fmt.Sprintf("%s (%d)", ui.StatusBadge(status), len(tickets))

	titleWidth := width - 15
	if titleWidth < 8 {
		titleWidth = 8
	}

	table := ui.NewTable([]string{"ID", "PRI", "TITLE"}, width)
	for _, t := range tickets {
		pri := "-"
		if t.Priority != 0 {
			pri = fmt.Sprintf("%d", t.Priority)
		}
		table.AddRow([]string{t.ID, pri, ui.TruncateString(t.Title, titleWidth)}, *rowIndex == bm.selectedIndex)
		(*rowIndex)++
	}

	return header + "\n" + table.Render()
}

// SelectedTicketRenderedLine returns the 0-indexed line within Render(width)'s
// output where the currently selected ticket's row is drawn, walking the
// same status-header/table-header/separator structure renderTicketGroup
// builds. Ticket indexes do not correspond 1:1 to rendered lines: a group
// contributes its own header line, the table's header and separator lines,
// and (for every group after the first) a blank line joining it to the
// previous section, before any of its ticket rows appear. ok is false when
// the board has no tickets to select.
func (bm *BoardModel) SelectedTicketRenderedLine(width int) (int, bool) {
	if bm.data == nil || len(bm.data.Tickets) == 0 {
		return 0, false
	}
	bm.clampSelection()

	line := 0
	rowIndex := 0
	first := true
	for _, status := range orderedStatuses(bm.data.Tickets) {
		tickets := bm.data.Tickets[status]
		if len(tickets) == 0 {
			continue
		}
		if !first {
			line++ // blank line strings.Join(sections, "\n\n") inserts
		}
		first = false
		line += 3 // group header + table header row + table separator row

		for range tickets {
			if rowIndex == bm.selectedIndex {
				return line, true
			}
			line++
			rowIndex++
		}
	}
	return 0, false
}

func (bm *BoardModel) orderedTickets() []client.Ticket {
	if bm.data == nil || len(bm.data.Tickets) == 0 {
		return nil
	}
	var tickets []client.Ticket
	for _, status := range orderedStatuses(bm.data.Tickets) {
		tickets = append(tickets, bm.data.Tickets[status]...)
	}
	return tickets
}

func (bm *BoardModel) clampSelection() {
	tickets := bm.orderedTickets()
	if len(tickets) == 0 {
		bm.selectedIndex = 0
		return
	}
	if bm.selectedIndex < 0 {
		bm.selectedIndex = 0
	}
	if bm.selectedIndex >= len(tickets) {
		bm.selectedIndex = len(tickets) - 1
	}
}
