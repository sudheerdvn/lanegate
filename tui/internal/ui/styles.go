package ui

import (
	"github.com/charmbracelet/lipgloss"
)

// Colors used throughout the UI
const (
	ColorPrimary   = lipgloss.Color("6")  // Cyan
	ColorSecondary = lipgloss.Color("5")  // Magenta
	ColorSuccess   = lipgloss.Color("2")  // Green
	ColorWarning   = lipgloss.Color("3")  // Yellow
	ColorError     = lipgloss.Color("1")  // Red
	ColorNeutral   = lipgloss.Color("8")  // Gray
	ColorSelected  = lipgloss.Color("11") // Bright yellow
)

// Styles for common elements
var (
	// Headers
	HeaderStyle = lipgloss.NewStyle().
			Bold(true).
			Foreground(ColorPrimary).
			PaddingBottom(1)

	// Borders
	BorderStyle = lipgloss.NewStyle().
			BorderForeground(ColorPrimary)

	// Selections
	SelectedItemStyle = lipgloss.NewStyle().
				Foreground(ColorSelected).
				Bold(true)

	// Status badges
	StatusOpenStyle = lipgloss.NewStyle().
			Foreground(ColorPrimary).
			Padding(0, 1)

	StatusInProgressStyle = lipgloss.NewStyle().
				Foreground(ColorWarning).
				Padding(0, 1)

	StatusCompleteStyle = lipgloss.NewStyle().
				Foreground(ColorSuccess).
				Padding(0, 1)

	StatusBlockedStyle = lipgloss.NewStyle().
				Foreground(ColorError).
				Padding(0, 1)

	// Field labels
	LabelStyle = lipgloss.NewStyle().
			Bold(true).
			Foreground(ColorSecondary)

	// Subtle text
	SubtleStyle = lipgloss.NewStyle().
			Foreground(ColorNeutral)
)

// StatusBadge returns a styled status string
func StatusBadge(status string) string {
	switch status {
	case "open":
		return StatusOpenStyle.Render("OPEN")
	case "in_progress":
		return StatusInProgressStyle.Render("IN PROGRESS")
	case "code_complete":
		return StatusCompleteStyle.Render("COMPLETE")
	case "in_review":
		return StatusCompleteStyle.Render("REVIEW")
	case "merged":
		return StatusSuccessStyle.Render("MERGED")
	case "blocked", "changes_requested":
		return StatusBlockedStyle.Render("BLOCKED")
	default:
		return SubtleStyle.Render(status)
	}
}

var StatusSuccessStyle = lipgloss.NewStyle().
	Foreground(ColorSuccess).
	Padding(0, 1)

// Semantic activity styles for the Run screen's structured Activity feed.
// Each style pairs with a durable text symbol from
// ActivitySymbol so an entry's meaning survives when color is unavailable or
// stripped (e.g. golden-file tests, non-color terminals).
var (
	ActivityActiveStyle  = lipgloss.NewStyle().Foreground(ColorPrimary)
	ActivityWaitingStyle = lipgloss.NewStyle().Foreground(ColorWarning)
	ActivitySuccessStyle = lipgloss.NewStyle().Foreground(ColorSuccess)
	ActivityDangerStyle  = lipgloss.NewStyle().Foreground(ColorError)
)

// ActivityCategory is one of the four semantic buckets the Run screen's
// Activity feed groups events into: "active" work in progress, "waiting"
// (retrying/reviewing/idle), "success", or "danger" (failure, blocked,
// hibernated).
type ActivityCategory string

const (
	ActivityCategoryActive  ActivityCategory = "active"
	ActivityCategoryWaiting ActivityCategory = "waiting"
	ActivityCategorySuccess ActivityCategory = "success"
	ActivityCategoryDanger  ActivityCategory = "danger"
)

// ActivityStyle returns the style for cat, defaulting to ActivityActiveStyle
// for an unrecognized category.
func ActivityStyle(cat ActivityCategory) lipgloss.Style {
	switch cat {
	case ActivityCategoryWaiting:
		return ActivityWaitingStyle
	case ActivityCategorySuccess:
		return ActivitySuccessStyle
	case ActivityCategoryDanger:
		return ActivityDangerStyle
	default:
		return ActivityActiveStyle
	}
}

// ActivitySymbol returns a durable, non-color text symbol for cat so status
// stays readable with color stripped.
func ActivitySymbol(cat ActivityCategory) string {
	switch cat {
	case ActivityCategoryWaiting:
		return "~"
	case ActivityCategorySuccess:
		return "✓"
	case ActivityCategoryDanger:
		return "✗"
	default:
		return "▶"
	}
}

// AuditStyle and AuditSymbol format presentation-only audit metadata returned
// by the API. They leave the raw audit message untouched, so copy/export
// retains the exact diagnostic text.
func AuditStyle(level string) lipgloss.Style {
	return ActivityStyle(auditCategory(level))
}

func AuditSymbol(level string) string {
	return ActivitySymbol(auditCategory(level))
}

func auditCategory(level string) ActivityCategory {
	switch level {
	case "error":
		return ActivityCategoryDanger
	case "warning":
		return ActivityCategoryWaiting
	case "success":
		return ActivityCategorySuccess
	default:
		return ActivityCategoryActive
	}
}
