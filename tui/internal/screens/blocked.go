package screens

import (
	"fmt"
	"strings"

	"lanegate/tui/internal/client"
	"lanegate/tui/internal/ui"
)

// BlockedModel represents the blocked queue screen state (MVP placeholder)
type BlockedModel struct {
	data          *client.BlockedPayload
	selectedIndex int
}

// NewBlockedModel creates a new blocked model
func NewBlockedModel() *BlockedModel {
	return &BlockedModel{
		data: &client.BlockedPayload{},
	}
}

// SetData updates the blocked data
func (bm *BlockedModel) SetData(data *client.BlockedPayload) {
	bm.data = data
	bm.clampSelection()
}

// GetData returns the blocked data
func (bm *BlockedModel) GetData() *client.BlockedPayload {
	return bm.data
}

// MoveSelection moves the active blocked-ticket row by delta.
func (bm *BlockedModel) MoveSelection(delta int) bool {
	if bm.data == nil || len(bm.data.Blocked) == 0 {
		bm.selectedIndex = 0
		return false
	}
	old := bm.selectedIndex
	bm.selectedIndex += delta
	bm.clampSelection()
	return bm.selectedIndex != old
}

// SelectedTicketID returns the active blocked ticket id, or "" when empty.
func (bm *BlockedModel) SelectedTicketID() string {
	if bm.data == nil || len(bm.data.Blocked) == 0 {
		return ""
	}
	bm.clampSelection()
	return bm.data.Blocked[bm.selectedIndex].ID
}

// SelectedIndex returns the active blocked-ticket index.
func (bm *BlockedModel) SelectedIndex() int {
	bm.clampSelection()
	return bm.selectedIndex
}

// Render renders the blocked/review queue screen as plain text sized to
// width. Each entry lists its id, title, branch, and review findings.
func (bm *BlockedModel) Render(width int) string {
	if bm.data == nil || len(bm.data.Blocked) == 0 {
		return "No blocked tickets."
	}

	titleWidth := width - len("TICK-000") - 2
	if titleWidth < 8 {
		titleWidth = 8
	}

	var sections []string
	for i, t := range bm.data.Blocked {
		var b strings.Builder
		title := fmt.Sprintf("%s  %s", t.ID, ui.TruncateString(t.Title, titleWidth))
		if i == bm.selectedIndex {
			title = ui.SelectedItemStyle.Render(title)
		}
		fmt.Fprintf(&b, "%s\n", title)
		fmt.Fprintf(&b, "%s %s\n", ui.LabelStyle.Render("Branch:"), t.Branch)

		if len(t.Findings) > 0 {
			b.WriteString(ui.LabelStyle.Render("Findings:"))
			b.WriteString("\n")
			for _, f := range t.Findings {
				b.WriteString(ui.WrapText("- "+f, width))
				b.WriteString("\n")
			}
		}

		sections = append(sections, strings.TrimRight(b.String(), "\n"))
	}

	return strings.Join(sections, "\n\n")
}

func (bm *BlockedModel) clampSelection() {
	if bm.data == nil || len(bm.data.Blocked) == 0 {
		bm.selectedIndex = 0
		return
	}
	if bm.selectedIndex < 0 {
		bm.selectedIndex = 0
	}
	if bm.selectedIndex >= len(bm.data.Blocked) {
		bm.selectedIndex = len(bm.data.Blocked) - 1
	}
}
