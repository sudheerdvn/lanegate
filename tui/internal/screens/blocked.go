package screens

import (
	"fmt"
	"strings"

	"lanegate/tui/internal/client"
	"lanegate/tui/internal/ui"
)

// BlockedModel represents tickets awaiting a human decision or intervention.
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

// Render renders the needs-human-decision queue grouped by remediation type.
func (bm *BlockedModel) Render(width int) string {
	if bm.data == nil || len(bm.data.Blocked) == 0 {
		return "No blocked tickets."
	}

	titleWidth := width - len("TICK-000") - 2
	if titleWidth < 8 {
		titleWidth = 8
	}

	categoryOrder := []struct {
		key, label string
	}{
		{"escalated", "Escalated"},
		{"rejected", "Changes Requested"},
		{"failed", "Failed"},
		{"stuck", "Stuck"},
		{"awaiting_merge", "Awaiting Merge"},
		{"", "Needs Attention"}, // compatibility with older API fixtures
	}
	grouped := make(map[string][]int, len(categoryOrder))
	for i, ticket := range bm.data.Blocked {
		grouped[ticket.AttentionCategory] = append(grouped[ticket.AttentionCategory], i)
	}

	var sections []string
	for _, category := range categoryOrder {
		indices := grouped[category.key]
		if len(indices) == 0 {
			continue
		}
		var rows []string
		for _, i := range indices {
			t := bm.data.Blocked[i]
			var b strings.Builder
			title := fmt.Sprintf("%s  %s", t.ID, ui.TruncateString(t.Title, titleWidth))
			if i == bm.selectedIndex {
				title = ui.SelectedItemStyle.Render(title)
			}
			fmt.Fprintf(&b, "%s\n", title)
			fmt.Fprintf(&b, "%s %s\n", ui.LabelStyle.Render("Branch:"), t.Branch)
			if t.AttentionSummary != "" {
				fmt.Fprintf(&b, "%s %s\n", ui.LabelStyle.Render("Reason:"), ui.WrapText(t.AttentionSummary, width))
			}

			if len(t.Findings) > 0 {
				b.WriteString(ui.LabelStyle.Render("Findings:"))
				b.WriteString("\n")
				for _, f := range t.Findings {
					b.WriteString(ui.WrapText("- "+f, width))
					b.WriteString("\n")
				}
			}

			rows = append(rows, strings.TrimRight(b.String(), "\n"))
		}
		sections = append(sections, ui.LabelStyle.Render(category.label)+"\n"+strings.Join(rows, "\n\n"))
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
