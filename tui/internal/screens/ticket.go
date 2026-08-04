package screens

import (
	"fmt"
	"strings"

	"lanegate/tui/internal/client"
	"lanegate/tui/internal/ui"
)

// TicketModel represents the ticket detail screen state (MVP placeholder)
type TicketModel struct {
	data *client.TicketDetail
}

// NewTicketModel creates a new ticket model
func NewTicketModel() *TicketModel {
	return &TicketModel{
		data: &client.TicketDetail{},
	}
}

// SetData updates the ticket data
func (tm *TicketModel) SetData(data *client.TicketDetail) {
	tm.data = data
}

// GetData returns the ticket data
func (tm *TicketModel) GetData() *client.TicketDetail {
	return tm.data
}

// Render renders the ticket detail screen as plain text sized to width.
func (tm *TicketModel) Render(width int) string {
	d := tm.data
	if d == nil || d.ID == "" {
		return "No ticket selected."
	}

	var b strings.Builder

	fmt.Fprintf(&b, "%s  %s\n", d.ID, d.Title)

	b.WriteString(ui.StatusBadge(d.Status))
	if d.ReviewVerdict != "" {
		fmt.Fprintf(&b, "  review: %s", d.ReviewVerdict)
	}
	b.WriteString("\n\n")

	if d.Milestone != "" {
		fmt.Fprintf(&b, "%s %s\n", ui.LabelStyle.Render("Milestone:"), d.Milestone)
	}
	if d.Branch != "" {
		fmt.Fprintf(&b, "%s %s\n", ui.LabelStyle.Render("Branch:"), d.Branch)
	}
	if len(d.Touches) > 0 {
		fmt.Fprintf(&b, "%s %s\n", ui.LabelStyle.Render("Touches:"), strings.Join(d.Touches, ", "))
	}

	if d.Body != "" {
		b.WriteString("\n")
		b.WriteString(ui.WrapText(d.Body, width))
		b.WriteString("\n")
	}

	if d.ReviewSummary != "" {
		b.WriteString("\n")
		b.WriteString(ui.LabelStyle.Render("Review Summary:"))
		b.WriteString("\n")
		b.WriteString(ui.WrapText(d.ReviewSummary, width))
		b.WriteString("\n")
	}

	if len(d.LifecycleEvents) > 0 {
		b.WriteString("\n")
		b.WriteString(ui.LabelStyle.Render("Lifecycle Timeline:"))
		b.WriteString("\n")
		start := len(d.LifecycleEvents) - 3
		if start < 0 {
			start = 0
		}
		for _, event := range d.LifecycleEvents[start:] {
			transition := event.ToStatus
			if event.FromStatus != "" && event.ToStatus != "" {
				transition = event.FromStatus + " → " + event.ToStatus
			}
			text := strings.TrimSpace(strings.Join([]string{transition, event.Summary}, " — "))
			b.WriteString(ui.WrapText("- "+text, width))
			b.WriteString("\n")
		}
	}

	if len(d.ReviewFindings) > 0 {
		b.WriteString("\n")
		b.WriteString(ui.LabelStyle.Render("Review Findings:"))
		b.WriteString("\n")
		for _, f := range d.ReviewFindings {
			b.WriteString(ui.WrapText("- "+f, width))
			b.WriteString("\n")
		}
	}

	return strings.TrimRight(b.String(), "\n")
}
