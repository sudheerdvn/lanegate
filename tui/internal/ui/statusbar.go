package ui

import (
	"fmt"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
)

// StatusBar holds status bar state
type StatusBar struct {
	ScreenName string
	HasHelp    bool
	Error      string
	Info       string
	// PageInfo is a short "where am I" fragment (e.g. a Raw Audit Log page
	// position) rendered right after ScreenName. Unlike Keys, it is
	// protected from truncation — see Render — since losing key hints to a
	// narrow width is a minor inconvenience, but a page position cut off
	// mid-word ("entrie...") is actively misleading.
	PageInfo string
	// AttentionCount is the number of tickets waiting for a human decision.
	// It is derived from the server-owned board predicate, not UI status names.
	AttentionCount int
	Keys           []string
}

// NewStatusBar creates a new status bar
func NewStatusBar() *StatusBar {
	return &StatusBar{
		ScreenName: "Board",
		HasHelp:    false,
		Error:      "",
		Info:       "",
		Keys:       nil,
	}
}

// Render returns a formatted status bar
func (sb *StatusBar) Render(width int) string {
	if width < 20 {
		return ""
	}

	style := lipgloss.NewStyle().
		Background(lipgloss.Color("240")).
		Foreground(lipgloss.Color("255")).
		Width(width).
		Padding(0, 1)

	var content string
	if sb.Error != "" {
		content = fmt.Sprintf("ERROR: %s", sb.Error)
		if len(content) > width-4 {
			content = content[:max(width-7, 0)] + "..."
		}
	} else if sb.Info != "" {
		content = sb.Info
		if len(content) > width-4 {
			content = content[:max(width-7, 0)] + "..."
		}
	} else {
		keys := sb.Keys
		if len(keys) == 0 && sb.HasHelp {
			keys = []string{"? help"}
		}
		prefix := fmt.Sprintf("  %s", sb.ScreenName)
		prefix += fmt.Sprintf("  attention: %d", sb.AttentionCount)
		if sb.PageInfo != "" {
			prefix += "  " + sb.PageInfo
		}
		keysStr := ""
		if len(keys) > 0 {
			keysStr = "  " + strings.Join(keys, " | ")
		}
		content = prefix + keysStr

		if len(content) > width-4 {
			// Truncate the key-hint list first — a shortened key list is a
			// minor inconvenience, but cutting into the screen name/page
			// position mid-word is confusing. Only fall back to a blunt cut
			// of the whole line once the prefix alone doesn't fit either.
			budget := max(width-7, 0) - len(prefix)
			if budget > 0 {
				if len(keysStr) > budget {
					keysStr = keysStr[:budget]
				}
				content = prefix + keysStr + "..."
			} else {
				content = prefix[:max(width-7, 0)] + "..."
			}
		}
	}

	return style.Render(content)
}

// SetScreen sets the screen name for the status bar
func (sb *StatusBar) SetScreen(name string) {
	sb.ScreenName = name
}

// SetKeys sets the key hints rendered in the default status bar state.
func (sb *StatusBar) SetKeys(keys []string) {
	sb.Keys = keys
	sb.HasHelp = true
}

// SetError sets an error message
func (sb *StatusBar) SetError(err string) {
	sb.Error = err
}

// ClearError clears any error message
func (sb *StatusBar) ClearError() {
	sb.Error = ""
}

// SetInfo sets an info message
func (sb *StatusBar) SetInfo(info string) {
	sb.Info = info
}

// ClearInfo clears any info message
func (sb *StatusBar) ClearInfo() {
	sb.Info = ""
}

// SetPageInfo sets the protected "where am I" fragment shown after ScreenName.
func (sb *StatusBar) SetPageInfo(info string) {
	sb.PageInfo = info
}

// SetAttentionCount updates the cross-screen needs-human-decision badge.
func (sb *StatusBar) SetAttentionCount(count int) {
	sb.AttentionCount = count
}

// FormatProgress returns a progress indicator string
func FormatProgress(current, total int) string {
	if total == 0 {
		return ""
	}
	percent := (current * 100) / total
	return fmt.Sprintf("%d/%d (%d%%)", current, total, percent)
}

// FormatDuration returns a formatted duration string
func FormatDuration(seconds int) string {
	if seconds < 60 {
		return fmt.Sprintf("%ds", seconds)
	}
	mins := seconds / 60
	secs := seconds % 60
	if secs == 0 {
		return fmt.Sprintf("%dm", mins)
	}
	return fmt.Sprintf("%dm%ds", mins, secs)
}

// FormatLocalTS converts a stored UTC "...Z" timestamp (as lanegate's Python
// core writes them) to the machine's local timezone for display — e.g.
// "2026-07-29T17:32:25Z" -> "2026-07-29 10:32:25 PDT". Returns the input
// unchanged if it isn't parseable, so a format drift degrades to raw text
// instead of hiding the value.
func FormatLocalTS(iso string) string {
	if iso == "" {
		return iso
	}
	t, err := time.Parse(time.RFC3339, iso)
	if err != nil {
		return iso
	}
	return t.Local().Format("2006-01-02 15:04:05 MST")
}

// FormatSize returns a human-readable file size
func FormatSize(bytes int64) string {
	units := []string{"B", "KB", "MB", "GB"}
	size := float64(bytes)
	for _, unit := range units {
		if size < 1024 {
			return fmt.Sprintf("%.1f%s", size, unit)
		}
		size /= 1024
	}
	return fmt.Sprintf("%.1fTB", size)
}

// WrapText wraps text to a given width
func WrapText(text string, width int) string {
	if width < 10 {
		return text
	}

	var lines []string
	for _, line := range strings.Split(text, "\n") {
		for len(line) > width {
			splitIdx := width
			for splitIdx > 0 && line[splitIdx] != ' ' {
				splitIdx--
			}
			if splitIdx == 0 {
				splitIdx = width
			}
			lines = append(lines, strings.TrimRight(line[:splitIdx], " "))
			line = strings.TrimLeft(line[splitIdx:], " ")
		}
		if len(line) > 0 {
			lines = append(lines, line)
		}
	}

	return strings.Join(lines, "\n")
}
