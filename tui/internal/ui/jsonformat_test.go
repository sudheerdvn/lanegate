package ui

import (
	"strings"
	"testing"
)

func TestFormatJSONIfValid(t *testing.T) {
	tests := []struct {
		name       string
		input      string
		wantOK     bool
		wantMulti  bool
		wantSubstr []string
	}{
		{
			name:       "valid object",
			input:      `{"a":1,"b":{"c":2}}`,
			wantOK:     true,
			wantMulti:  true,
			wantSubstr: []string{`"a"`, `1`, `"b"`, `"c"`, `2`},
		},
		{
			name:       "valid array",
			input:      `[1,2,3]`,
			wantOK:     true,
			wantMulti:  true,
			wantSubstr: []string{"1", "2", "3"},
		},
		{
			name:       "valid string literal",
			input:      `"hello"`,
			wantOK:     true,
			wantMulti:  false, // a bare scalar has nothing to indent
			wantSubstr: []string{"hello"},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, ok := FormatJSONIfValid(tt.input)
			if ok != tt.wantOK {
				t.Fatalf("ok = %v, want %v", ok, tt.wantOK)
			}
			if tt.wantMulti && !strings.Contains(got, "\n") {
				t.Errorf("expected indented multi-line output, got %q", got)
			}
			for _, sub := range tt.wantSubstr {
				if !strings.Contains(got, sub) {
					t.Errorf("output missing substring %q: %q", sub, got)
				}
			}
			// All original bytes must be recoverable once whitespace is stripped.
			stripped := strings.Join(strings.Fields(got), "")
			wantStripped := strings.Join(strings.Fields(tt.input), "")
			if stripped != wantStripped {
				t.Errorf("stripped output %q != stripped input %q", stripped, wantStripped)
			}
		})
	}
}

func TestFormatJSONIfValid_NonJSON(t *testing.T) {
	input := "raw executor protocol line"
	got, ok := FormatJSONIfValid(input)
	if ok {
		t.Fatalf("expected ok=false for non-JSON text")
	}
	if got != input {
		t.Errorf("expected unchanged text, got %q, want %q", got, input)
	}
}

func TestFormatJSONIfValid_Empty(t *testing.T) {
	got, ok := FormatJSONIfValid("")
	if ok {
		t.Fatalf("expected ok=false for empty text")
	}
	if got != "" {
		t.Errorf("expected unchanged empty text, got %q", got)
	}
}

func TestWrapJSONAware_JSON(t *testing.T) {
	input := `{"tool":"edit","args":{"file":"x.py","line":10}}`
	got := WrapJSONAware(input, 20)
	if !strings.Contains(got, "\n") {
		t.Fatalf("expected multi-line indented output at narrow width, got %q", got)
	}
	if !strings.Contains(got, `"tool"`) || !strings.Contains(got, `"args"`) {
		t.Errorf("expected key fragments present, got %q", got)
	}
	lines := strings.Split(got, "\n")
	if len(lines) < 3 {
		t.Errorf("expected several indented lines, got %d: %q", len(lines), got)
	}
}

func TestWrapJSONAware_NonJSON(t *testing.T) {
	input := "raw executor protocol line that is definitely longer than the wrap width used in this test"
	width := 20
	got := WrapJSONAware(input, width)
	want := WrapText(input, width)
	if got != want {
		t.Errorf("WrapJSONAware fallback diverged from WrapText:\ngot:  %q\nwant: %q", got, want)
	}
}
