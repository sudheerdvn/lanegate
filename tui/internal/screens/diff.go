package screens

import (
	"fmt"
	"strings"

	"lanegate/tui/internal/client"
	"lanegate/tui/internal/ui"
)

// DiffModel represents the diff view screen state.
type DiffModel struct {
	data *client.DiffPayload
}

// NewDiffModel creates a new diff model
func NewDiffModel() *DiffModel {
	return &DiffModel{
		data: &client.DiffPayload{},
	}
}

// SetData updates the diff data
func (dm *DiffModel) SetData(data *client.DiffPayload) {
	dm.data = data
}

// GetData returns the diff data
func (dm *DiffModel) GetData() *client.DiffPayload {
	return dm.data
}

// isBinaryPatch reports whether a file's patch text is git's binary-file
// marker rather than a line-level diff, so Render can show a short notice
// instead of dumping that marker text as if it were a patch.
func isBinaryPatch(patch string) bool {
	return strings.Contains(patch, "Binary files") && strings.Contains(patch, "differ")
}

// Render renders the diff screen as plain text sized to width. Patches are
// already bounded server-side (see lanegate.ticket.get_ticket_diff); this only
// adds a visible marker for truncated/binary entries rather than
// re-truncating already-bounded text.
func (dm *DiffModel) Render(width int) string {
	d := dm.data
	if d == nil || d.ID == "" {
		return "No diff available."
	}

	if d.Error != "" {
		return ui.LabelStyle.Render("Error:") + " " + d.Error
	}

	var b strings.Builder
	fmt.Fprintf(&b, "%s  %s -> %s\n", d.ID, d.Base, d.Branch)

	if d.Stat != "" {
		b.WriteString(ui.WrapText(strings.TrimRight(d.Stat, "\n"), width))
		b.WriteString("\n")
	}

	if len(d.Files) == 0 {
		b.WriteString("\nNo changed files.")
		return strings.TrimRight(b.String(), "\n")
	}

	for _, f := range d.Files {
		b.WriteString("\n")
		header := fmt.Sprintf("%s  %s", f.Status, f.Path)
		if f.OldPath != "" {
			header = fmt.Sprintf("%s  %s -> %s", f.Status, f.OldPath, f.Path)
		}
		b.WriteString(ui.LabelStyle.Render(header))
		b.WriteString("\n")

		switch {
		case f.Error != "":
			b.WriteString(ui.WrapText("error: "+f.Error, width))
			b.WriteString("\n")
		case isBinaryPatch(f.Patch):
			b.WriteString("[binary file — no diff shown]\n")
		case f.Patch == "":
			b.WriteString("(no changes)\n")
		default:
			b.WriteString(ui.WrapText(strings.TrimRight(f.Patch, "\n"), width))
			b.WriteString("\n")
			if f.Truncated {
				b.WriteString(ui.SubtleStyle.Render("(patch truncated)"))
				b.WriteString("\n")
			}
		}
	}

	return strings.TrimRight(b.String(), "\n")
}
