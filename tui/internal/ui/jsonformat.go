package ui

import (
	"bytes"
	"encoding/json"
	"strings"
)

// FormatJSONIfValid pretty-prints text if it is valid JSON (object, array,
// string, number, bool, or null). It returns the original text unchanged
// with ok=false if text is empty or not valid JSON.
func FormatJSONIfValid(text string) (string, bool) {
	trimmed := strings.TrimSpace(text)
	if trimmed == "" {
		return text, false
	}

	var buf bytes.Buffer
	if err := json.Indent(&buf, []byte(trimmed), "", "  "); err != nil {
		return text, false
	}
	return buf.String(), true
}

// WrapJSONAware pretty-prints text as indented JSON when it is valid JSON,
// then wraps the result (JSON or not) through WrapText so no line exceeds
// width.
func WrapJSONAware(text string, width int) string {
	formatted, ok := FormatJSONIfValid(text)
	if !ok {
		return WrapText(text, width)
	}
	return WrapText(formatted, width)
}
