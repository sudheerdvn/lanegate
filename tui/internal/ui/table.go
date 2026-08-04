package ui

import (
	"fmt"
	"strings"
)

// TableRow represents a single row in a table
type TableRow struct {
	Columns []string
	Selected bool
}

// Table represents a simple table for rendering
type Table struct {
	Headers []string
	Rows    []TableRow
	Width   int
}

// NewTable creates a new table with the given headers
func NewTable(headers []string, width int) *Table {
	return &Table{
		Headers: headers,
		Rows:    []TableRow{},
		Width:   width,
	}
}

// AddRow adds a row to the table
func (t *Table) AddRow(columns []string, selected bool) {
	t.Rows = append(t.Rows, TableRow{
		Columns: columns,
		Selected: selected,
	})
}

// Render returns the table as a formatted string
func (t *Table) Render() string {
	if len(t.Rows) == 0 {
		return ""
	}

	// Calculate column widths
	colWidths := t.calculateColumnWidths()

	// Render header
	var lines []string
	headerLine := t.formatRow(t.Headers, colWidths, false)
	lines = append(lines, headerLine)
	lines = append(lines, t.renderSeparator(colWidths))

	// Render rows
	for _, row := range t.Rows {
		line := t.formatRow(row.Columns, colWidths, row.Selected)
		lines = append(lines, line)
	}

	return strings.Join(lines, "\n")
}

func (t *Table) calculateColumnWidths() []int {
	widths := make([]int, len(t.Headers))

	// Start with header widths
	for i, header := range t.Headers {
		widths[i] = len(header)
	}

	// Expand for content
	for _, row := range t.Rows {
		for i, col := range row.Columns {
			if i < len(widths) && len(col) > widths[i] {
				widths[i] = len(col)
			}
		}
	}

	return widths
}

func (t *Table) formatRow(columns []string, widths []int, selected bool) string {
	var parts []string
	for i, col := range columns {
		if i < len(widths) {
			parts = append(parts, fmt.Sprintf("%-*s", widths[i], col))
		}
	}
	line := strings.Join(parts, "  ")

	if selected {
		return SelectedItemStyle.Render(line)
	}
	return line
}

func (t *Table) renderSeparator(widths []int) string {
	var parts []string
	for _, w := range widths {
		parts = append(parts, strings.Repeat("─", w))
	}
	return strings.Join(parts, "  ")
}

// TruncateString truncates a string to fit in width, adding ellipsis if needed
func TruncateString(s string, width int) string {
	if len(s) <= width {
		return s
	}
	if width <= 3 {
		return s[:width]
	}
	return s[:width-3] + "..."
}

// PadString pads a string to a given width
func PadString(s string, width int) string {
	if len(s) >= width {
		return s
	}
	return s + strings.Repeat(" ", width-len(s))
}
