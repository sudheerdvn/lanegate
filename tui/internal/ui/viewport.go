package ui

import (
	"strings"
)

// Viewport provides utilities for scrollable content
type Viewport struct {
	lines  []string
	offset int
	width  int
	height int
}

// NewViewport creates a new viewport
func NewViewport(width, height int) *Viewport {
	v := &Viewport{lines: []string{}}
	v.SetSize(width, height)
	return v
}

// ContentHeight returns how many rows are available for scrollable content
// once footer is reserved from totalHeight. footer is the exact text that
// will be rendered below the content (e.g. "\n\n" + statusBar.Render(...));
// its actual line count — not a hardcoded constant — determines how much is
// reserved, so a footer that grows past one rendered line still gets fully
// accounted for. Never negative: at constrained heights where footer alone
// would exceed totalHeight, callers get 0 rather than a value that causes
// content+footer to render more rows than the window has.
func ContentHeight(totalHeight int, footer string) int {
	reserved := strings.Count(footer, "\n")
	height := totalHeight - reserved
	if height < 0 {
		return 0
	}
	return height
}

// SetContent sets the content to display
func (v *Viewport) SetContent(content string) {
	v.lines = strings.Split(content, "\n")
	v.offset = 0
}

// SetSize updates viewport dimensions and clamps the current offset.
func (v *Viewport) SetSize(width, height int) {
	v.width = width
	v.height = height
	v.SetOffset(v.offset)
}

// SetOffset moves to an absolute line offset, clamped to valid content bounds.
func (v *Viewport) SetOffset(offset int) {
	if offset < 0 {
		offset = 0
	}
	maxOffset := v.MaxOffset()
	if offset > maxOffset {
		offset = maxOffset
	}
	v.offset = offset
}

// Offset returns the current scroll offset.
func (v *Viewport) Offset() int {
	return v.offset
}

// MaxOffset returns the largest valid scroll offset.
func (v *Viewport) MaxOffset() int {
	maxOffset := len(v.lines) - v.height
	if maxOffset < 0 {
		return 0
	}
	return maxOffset
}

// LineCount returns the number of content lines.
func (v *Viewport) LineCount() int {
	return len(v.lines)
}

// ScrollUp scrolls the viewport up
func (v *Viewport) ScrollUp(lines int) {
	v.offset -= lines
	if v.offset < 0 {
		v.offset = 0
	}
}

// ScrollDown scrolls the viewport down
func (v *Viewport) ScrollDown(lines int) {
	v.offset += lines
	if v.offset > v.MaxOffset() {
		v.offset = v.MaxOffset()
	}
}

// Home moves to the beginning
func (v *Viewport) Home() {
	v.offset = 0
}

// End moves to the end
func (v *Viewport) End() {
	v.offset = v.MaxOffset()
}

// Render returns the viewport content as a string
func (v *Viewport) Render() string {
	end := v.offset + v.height
	if end > len(v.lines) {
		end = len(v.lines)
	}

	if v.offset >= len(v.lines) || v.offset < 0 {
		return ""
	}

	visible := v.lines[v.offset:end]
	return strings.Join(visible, "\n")
}

// IsAtEnd returns whether the viewport is at the end
func (v *Viewport) IsAtEnd() bool {
	return v.offset+v.height >= len(v.lines)
}

// IsAtStart returns whether the viewport is at the start
func (v *Viewport) IsAtStart() bool {
	return v.offset == 0
}
