package screens

import (
	"fmt"
	"sort"
	"strings"

	"lanegate/tui/internal/client"
	"lanegate/tui/internal/ui"
)

// SettingsModel represents the settings/config preview screen state. Most of
// it is read-only by design (see TICK-157 non-goals): a rendering of GET
// /api/config's sanitized response. TICK-269 adds one narrow editing
// surface on top of that — a pools.<name>.executors reorder control — since
// tie-break preference and round-robin start order otherwise require
// hand-editing .lanegate.yml.
type SettingsModel struct {
	data *client.SettingsPayload
	err  string

	pools    []client.Pool
	poolsErr string

	// poolIndex/executorIndex select the focused pool and, while editingPool
	// is true, the focused executor row within pendingOrder.
	poolIndex     int
	executorIndex int
	editingPool   bool
	pendingOrder  []string
}

// NewSettingsModel creates a new settings model
func NewSettingsModel() *SettingsModel {
	return &SettingsModel{
		data: &client.SettingsPayload{},
	}
}

// SetData updates the settings data and clears any prior fetch error.
func (sm *SettingsModel) SetData(data *client.SettingsPayload) {
	sm.data = data
	sm.err = ""
}

// GetData returns the settings data
func (sm *SettingsModel) GetData() *client.SettingsPayload {
	return sm.data
}

// SetError records a failed GET /api/config fetch so Render can surface it.
// Passing nil clears it.
func (sm *SettingsModel) SetError(err error) {
	if err == nil {
		sm.err = ""
		return
	}
	sm.err = err.Error()
}

// SetPools updates the pools.<name>.executors view backing GET /api/pools
// and clears any prior fetch error. A refresh while a reorder is in
// progress abandons the in-flight edit rather than risk silently
// overwriting it with stale server state.
func (sm *SettingsModel) SetPools(pools []client.Pool) {
	sm.pools = pools
	sm.poolsErr = ""
	sm.editingPool = false
	sm.pendingOrder = nil
	if sm.poolIndex >= len(pools) {
		sm.poolIndex = 0
	}
	if sm.poolIndex < 0 {
		sm.poolIndex = 0
	}
}

// GetPools returns the current pools view.
func (sm *SettingsModel) GetPools() []client.Pool {
	return sm.pools
}

// SetPoolsError records a failed GET /api/pools fetch. Passing nil clears
// it.
func (sm *SettingsModel) SetPoolsError(err error) {
	if err == nil {
		sm.poolsErr = ""
		return
	}
	sm.poolsErr = err.Error()
}

// SelectedPool returns the currently focused pool, or (zero, false) when no
// pools are loaded.
func (sm *SettingsModel) SelectedPool() (client.Pool, bool) {
	if sm.poolIndex < 0 || sm.poolIndex >= len(sm.pools) {
		return client.Pool{}, false
	}
	return sm.pools[sm.poolIndex], true
}

// MovePoolSelection moves the focused pool by delta, clamped to the pool
// list. No-op while a reorder is in progress.
func (sm *SettingsModel) MovePoolSelection(delta int) bool {
	if sm.editingPool || len(sm.pools) == 0 {
		return false
	}
	old := sm.poolIndex
	sm.poolIndex += delta
	if sm.poolIndex < 0 {
		sm.poolIndex = 0
	}
	if sm.poolIndex >= len(sm.pools) {
		sm.poolIndex = len(sm.pools) - 1
	}
	return sm.poolIndex != old
}

// IsEditingPool reports whether a reorder is in progress.
func (sm *SettingsModel) IsEditingPool() bool {
	return sm.editingPool
}

// BeginPoolEdit starts reordering the focused pool's executors, seeding the
// working copy from its current order. Returns false when there is no pool
// to edit.
func (sm *SettingsModel) BeginPoolEdit() bool {
	pool, ok := sm.SelectedPool()
	if !ok {
		return false
	}
	sm.editingPool = true
	sm.pendingOrder = append([]string(nil), pool.Executors...)
	sm.executorIndex = 0
	return true
}

// CancelPoolEdit discards the in-progress reorder without saving.
func (sm *SettingsModel) CancelPoolEdit() {
	sm.editingPool = false
	sm.pendingOrder = nil
}

// PendingOrder returns the working (not-yet-saved) executor order while
// editing.
func (sm *SettingsModel) PendingOrder() []string {
	return append([]string(nil), sm.pendingOrder...)
}

// CommitPoolEdit applies a server-confirmed executors order to the focused
// pool and ends the reorder. Call this after a successful
// PUT /api/pools/{name}/executors.
func (sm *SettingsModel) CommitPoolEdit(executors []string) {
	if sm.poolIndex >= 0 && sm.poolIndex < len(sm.pools) {
		sm.pools[sm.poolIndex].Executors = executors
	}
	sm.editingPool = false
	sm.pendingOrder = nil
}

// MoveExecutorSelection moves the highlighted row within pendingOrder by
// delta, clamped to its bounds. No-op unless editing.
func (sm *SettingsModel) MoveExecutorSelection(delta int) bool {
	if !sm.editingPool || len(sm.pendingOrder) == 0 {
		return false
	}
	old := sm.executorIndex
	sm.executorIndex += delta
	if sm.executorIndex < 0 {
		sm.executorIndex = 0
	}
	if sm.executorIndex >= len(sm.pendingOrder) {
		sm.executorIndex = len(sm.pendingOrder) - 1
	}
	return sm.executorIndex != old
}

// MoveExecutor swaps the highlighted executor with its neighbor delta rows
// away (delta is typically -1 or +1) and moves the highlight with it. No-op
// unless editing or when the swap would go out of bounds.
func (sm *SettingsModel) MoveExecutor(delta int) bool {
	if !sm.editingPool {
		return false
	}
	target := sm.executorIndex + delta
	if target < 0 || target >= len(sm.pendingOrder) {
		return false
	}
	sm.pendingOrder[sm.executorIndex], sm.pendingOrder[target] = sm.pendingOrder[target], sm.pendingOrder[sm.executorIndex]
	sm.executorIndex = target
	return true
}

// sortedStringKeys returns m's keys sorted, so rendering a map field never
// leaks Go's randomized map iteration order into golden output.
func sortedStringKeys(m map[string]interface{}) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// Render renders the settings screen as plain text sized to width.
func (sm *SettingsModel) Render(width int) string {
	if sm.err != "" {
		return ui.LabelStyle.Render("Error:") + " " + sm.err
	}

	d := sm.data
	if d == nil || d.RepoRoot == "" {
		return "No settings available."
	}

	var b strings.Builder

	b.WriteString(ui.LabelStyle.Render("Repository"))
	b.WriteString("\n")
	fmt.Fprintf(&b, "Root:        %s\n", d.RepoRoot)
	fmt.Fprintf(&b, "Tickets dir: %s\n", d.TicketsDir)
	fmt.Fprintf(&b, "Worktrees:   %s\n", d.WorktreesDir)
	fmt.Fprintf(&b, "Prefix:      %s\n", d.TicketPrefix)

	b.WriteString("\n")
	b.WriteString(ui.LabelStyle.Render("Execution"))
	b.WriteString("\n")
	fmt.Fprintf(&b, "Executor:      %s\n", d.Executor)
	fmt.Fprintf(&b, "Max parallel:  %d\n", d.MaxParallel)
	if d.DefaultMilestone != "" {
		fmt.Fprintf(&b, "Milestone:     %s\n", d.DefaultMilestone)
	}
	fmt.Fprintf(&b, "On rate limit: %s\n", d.OnRateLimit)
	fmt.Fprintf(&b, "GitHub PR:     %t\n", d.GithubPR)
	fmt.Fprintf(&b, "Commit status: %t\n", d.CommitStatusChanges)

	if len(d.Models) > 0 {
		b.WriteString("\n")
		b.WriteString(ui.LabelStyle.Render("Models"))
		b.WriteString("\n")
		for _, k := range sortedStringKeys(d.Models) {
			fmt.Fprintf(&b, "%s: %v\n", k, d.Models[k])
		}
	}

	if len(d.Executors) > 0 {
		b.WriteString("\n")
		b.WriteString(ui.LabelStyle.Render("Executors"))
		b.WriteString("\n")
		for _, k := range sortedStringKeys(d.Executors) {
			fmt.Fprintf(&b, "%s\n", k)
		}
	}

	if len(d.Environments) > 0 {
		b.WriteString("\n")
		b.WriteString(ui.LabelStyle.Render("Environments"))
		b.WriteString("\n")
		table := ui.NewTable([]string{"ENV", "BRANCH", "FROM", "TRIGGER", "SYNC"}, width)
		for _, e := range d.Environments {
			table.AddRow([]string{e.Name, e.Branch, e.From, e.Trigger, e.Sync}, false)
		}
		b.WriteString(table.Render())
		b.WriteString("\n")
	}

	b.WriteString("\n")
	fmt.Fprintf(&b, "%s %s:%d\n", ui.LabelStyle.Render("API:"), d.API.Host, d.API.Port)

	sm.renderPools(&b)

	return strings.TrimRight(b.String(), "\n")
}

// renderPools appends the pools.<name>.executors view (TICK-269), including
// live dispatch state, the focused pool/executor cursor, and — while
// editing — the "p to reorder / enter to save / esc to cancel" hint. It is
// silent when no pools are configured and no pools fetch was attempted, so
// projects without a pools: block see no change from the pre-TICK-269
// settings screen.
func (sm *SettingsModel) renderPools(b *strings.Builder) {
	if sm.poolsErr != "" {
		b.WriteString("\n")
		b.WriteString(ui.LabelStyle.Render("Pools"))
		b.WriteString("\n")
		fmt.Fprintf(b, "Error: %s\n", sm.poolsErr)
		return
	}
	if len(sm.pools) == 0 {
		return
	}

	b.WriteString("\n")
	b.WriteString(ui.LabelStyle.Render("Pools"))
	b.WriteString("\n")

	for pi, p := range sm.pools {
		poolLine := fmt.Sprintf("%s (%s)", p.Name, p.Strategy)
		if p.Default {
			poolLine += "  [default]"
		}
		if pi == sm.poolIndex {
			b.WriteString(ui.SelectedItemStyle.Render("> " + poolLine))
		} else {
			b.WriteString("  " + poolLine)
		}
		b.WriteString("\n")

		executors := p.Executors
		if pi == sm.poolIndex && sm.editingPool {
			executors = sm.pendingOrder
		}
		for ei, ex := range executors {
			line := fmt.Sprintf("%d. %s", ei+1, ex)
			if count, ok := p.DispatchCounts[ex]; ok {
				line += fmt.Sprintf("  dispatched: %d", count)
			}
			if pi == sm.poolIndex && sm.editingPool && ei == sm.executorIndex {
				b.WriteString("    " + ui.SelectedItemStyle.Render("* "+line))
			} else {
				b.WriteString("      " + line)
			}
			b.WriteString("\n")
		}
	}

	if sm.editingPool {
		b.WriteString(ui.SubtleStyle.Render("Reordering — up/down select, J/K move, enter save, esc cancel"))
	} else {
		b.WriteString(ui.SubtleStyle.Render("p to reorder the selected pool's executors"))
	}
	b.WriteString("\n")
}
