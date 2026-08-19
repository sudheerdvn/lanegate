package app

import (
	"fmt"
	"strings"

	"github.com/charmbracelet/lipgloss"

	"lanegate/tui/internal/ui"
)

// View renders the UI
func (m *Model) View() string {
	clipboardEscape := m.consumeClipboardEscape()
	if !m.ready {
		return clipboardEscape + "Loading..."
	}

	body := m.currentBody()
	if m.helpVisible {
		body = m.helpBody()
	}

	vp := m.viewportFor(body)
	if !m.helpVisible {
		vp.SetOffset(m.scrollOffsets[m.screen])
	}

	m.statusBar.SetKeys(m.footerKeys(vp))
	m.statusBar.SetAttentionCount(m.needsAttentionCount())
	m.statusBar.SetPageInfo(m.footerPageInfo())
	statusBar := m.statusBar.Render(m.width)
	sticky := m.stickyFooter()

	bodyOut := vp.Render()
	switch {
	case bodyOut == "" && sticky == "":
		// At a terminal height too constrained to reserve any content rows
		// (bodyHeight() clamped to 0), skip the blank separator line: body +
		// "\n\n" would still render 2 rows for zero lines of actual content,
		// which is exactly the reservation the footer was already sized
		// against and would push the status bar itself off-screen.
		return clipboardEscape + statusBar
	case bodyOut == "":
		return clipboardEscape + sticky + "\n\n" + statusBar
	case sticky == "":
		return clipboardEscape + bodyOut + "\n\n" + statusBar
	default:
		return clipboardEscape + bodyOut + "\n\n" + sticky + "\n\n" + statusBar
	}
}

// stickyFooter returns content pinned between the scrollable body and the
// status bar, outside the scrolled region entirely. Only the Run History
// list screen uses this today: with many runs, the selected run's detail
// used to be the last thing in the same scrolled block as the table, so
// scrolling far enough to bring a row into view could push the detail (or,
// scrolling the other way, the table's own header) off-screen instead of
// leaving both visible at once. See screens.RunModel.RenderHistorySelectedDetail.
func (m *Model) stickyFooter() string {
	if m.helpVisible || m.loading {
		return ""
	}
	if m.screen == screenHistory && !m.run.IsHistoryDetail() {
		return m.run.RenderHistorySelectedDetail(m.width)
	}
	return ""
}

// needsAttentionCount uses the server-provided predicate flag in the Board
// payload, so the TUI never reimplements hibernation or review policy.
func (m *Model) needsAttentionCount() int {
	if m.board.GetData() == nil {
		return 0
	}
	count := 0
	for _, tickets := range m.board.GetData().Tickets {
		for _, ticket := range tickets {
			if ticket.NeedsAttention {
				count++
			}
		}
	}
	return count
}

func (m *Model) currentBody() string {
	switch {
	case m.loading:
		return fmt.Sprintf("Loading %s...", screenName(m.screen))
	case m.screen == screenTicket:
		return m.ticket.Render(m.width)
	case m.screen == screenBlocked:
		return m.blocked.Render(m.width)
	case m.screen == screenDiff:
		return m.diff.Render(m.width)
	case m.screen == screenRun:
		return m.run.Render(m.width)
	case m.screen == screenHistory && !m.run.IsHistoryDetail():
		// The selected run's detail renders separately, pinned by
		// stickyFooter instead of scrolling inside this table. See
		// RenderHistoryTable.
		return m.run.RenderHistoryTable(m.width)
	case m.screen == screenHistory:
		return m.run.RenderHistory(m.width)
	case m.screen == screenSettings:
		return m.settings.Render(m.width)
	default:
		return m.board.Render(m.width)
	}
}

func (m *Model) viewportFor(body string) *ui.Viewport {
	vp := ui.NewViewport(m.width, m.bodyHeight())
	vp.SetContent(body)
	return vp
}

// bodyHeight returns how many rows the content viewport gets once the
// footer View() appends below it ("\n\n" + the status bar) is reserved. The
// reservation is derived from the footer's actual rendered line count
// rather than a hardcoded constant, and is allowed to reach 0 at very
// constrained terminal heights instead of flooring at 1 — flooring there
// used to make body+footer add up to more rows than the terminal has,
// which pushed content (including the footer itself) off-screen.
func (m *Model) bodyHeight() int {
	footer := "\n\n" + m.statusBar.Render(m.width)
	if sticky := m.stickyFooter(); sticky != "" {
		footer = "\n\n" + sticky + footer
	}
	return ui.ContentHeight(m.height, footer)
}

func (m *Model) footerKeys(vp *ui.Viewport) []string {
	if m.helpVisible {
		return []string{"? close", "esc close", "q quit"}
	}

	keys := []string{"1-7 nav"}
	switch m.screen {
	case screenBoard, screenBlocked:
		keys = append(keys, "j/k select")
		if m.screen == screenBoard {
			keys = append(keys, "m group by milestone")
		}
	case screenRun:
		keys = append(keys, "j/k scroll", "a activity/audit", "c copy all", "v/y copy range")
		if m.run.IsAuditMode() {
			keys = append(keys, "n/N page")
		}
	case screenHistory:
		if m.run.IsHistoryDetail() {
			keys = append(keys, "j/k scroll", "a activity/audit", "c copy all", "v/y copy range")
		} else {
			keys = append(keys, "j/k select", "enter open")
		}
		if m.run.IsAuditMode() {
			keys = append(keys, "n/N page")
		}
	case screenSettings:
		keys = append(keys, "j/k select")
		if m.settings.IsEditingPool() {
			keys = append(keys, "J/K move", "enter save")
		} else {
			keys = append(keys, "p reorder pool")
		}
	default:
		keys = append(keys, "j/k scroll")
	}
	if vp.MaxOffset() > 0 {
		keys = append(keys, "pg scroll", "home/end")
	}
	keys = append(keys, "? help", "r refresh", "esc back", "q quit")
	return keys
}

// footerPageInfo returns the protected "where am I" fragment for the status
// bar (see ui.StatusBar.PageInfo) — currently just the Raw Audit Log page
// position, since that's the one place scrolling can hide where you are.
func (m *Model) footerPageInfo() string {
	if m.helpVisible || !m.runPaneVisible() || !m.run.IsAuditMode() {
		return ""
	}
	n := len(m.run.AuditEvents())
	if n == 0 {
		return ""
	}
	end := m.run.AuditOffset() + n
	return fmt.Sprintf("entries %d-%d/%d", m.run.AuditOffset()+1, end, m.run.AuditTotal())
}

func (m *Model) helpBody() string {
	var b strings.Builder
	title := "Help"
	if m.width >= 20 {
		title = lipgloss.NewStyle().Bold(true).Render(title)
	}
	b.WriteString(title)
	b.WriteString("\n\n")
	b.WriteString("Global\n")
	b.WriteString("  1-7        switch screens\n")
	b.WriteString("  q, ctrl+c  quit\n")
	b.WriteString("  r          refresh current screen\n")
	b.WriteString("  esc        close help, then go back\n")
	b.WriteString("  ?          toggle help\n")
	b.WriteString("  tab        pane focus (not implemented)\n")
	b.WriteString("  shift+tab  pane focus (not implemented)\n")
	b.WriteString("\n")
	b.WriteString(screenName(m.screen))
	b.WriteString("\n")
	switch m.screen {
	case screenBoard:
		b.WriteString("  up/down, j/k       move selected ticket\n")
		b.WriteString("  m                   toggle status / milestone grouping\n")
		b.WriteString("  pgup/pgdn          scroll board\n")
		b.WriteString("  home/end           jump to top or bottom\n")
		b.WriteString("  /                  filter (not implemented)\n")
	case screenTicket:
		b.WriteString("  up/down, j/k       scroll ticket body\n")
		b.WriteString("  pgup/pgdn          scroll ticket body\n")
		b.WriteString("  home/end           jump to top or bottom\n")
	case screenBlocked:
		b.WriteString("  up/down, j/k       move selected blocked ticket\n")
		b.WriteString("  pgup/pgdn          scroll blocked queue\n")
		b.WriteString("  home/end           jump to top or bottom\n")
	case screenDiff:
		b.WriteString("  up/down, j/k       scroll diff\n")
		b.WriteString("  pgup/pgdn          scroll diff\n")
		b.WriteString("  home/end           jump to top or bottom\n")
	case screenRun:
		b.WriteString("  up/down, j/k       scroll the live Run pane\n")
		b.WriteString("  a                  toggle structured Activity / Raw Audit Log\n")
		b.WriteString("  c                  copy all loaded Activity (or current Raw Audit Log page)\n")
		b.WriteString("  v, then y          mark copy-range start, scroll, then copy through this page\n")
		b.WriteString("  n / N              next / previous Raw Audit Log page\n")
		b.WriteString("  pgup/pgdn          scroll the active Run pane\n")
		b.WriteString("  home/end           jump to top or bottom\n")
	case screenHistory:
		b.WriteString("  up/down, j/k       select a historical run (or scroll an opened run)\n")
		b.WriteString("  enter              open the selected historical run\n")
		b.WriteString("  a                  toggle structured Activity / Raw Audit Log\n")
		b.WriteString("  c                  copy all loaded Activity (or current Raw Audit Log page)\n")
		b.WriteString("  v, then y          mark copy-range start, scroll, then copy through this page\n")
		b.WriteString("  n / N              next / previous Raw Audit Log page\n")
		b.WriteString("  pgup/pgdn          scroll the opened historical run\n")
		b.WriteString("  home/end           jump to top or bottom\n")
	case screenSettings:
		b.WriteString("  up/down, j/k       select pool (or executor row while reordering)\n")
		b.WriteString("  pgup/pgdn          scroll settings\n")
		b.WriteString("  home/end           jump to top or bottom\n")
		b.WriteString("  p                  reorder the selected pool's executors\n")
		b.WriteString("  J/K                move the highlighted executor while reordering\n")
		b.WriteString("  enter              save the reordered executors to .lanegate.yml\n")
		b.WriteString("  esc                cancel reordering (else: go back)\n")
	}
	return strings.TrimRight(b.String(), "\n")
}
