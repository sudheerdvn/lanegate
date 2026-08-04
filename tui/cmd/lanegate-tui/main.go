package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"

	tea "github.com/charmbracelet/bubbletea"
	"lanegate/tui/internal/app"
	"lanegate/tui/internal/client"
)

func main() {
	fixtureDir := flag.String("fixture-dir", "", "Directory containing fixture files for offline mode")
	fixture := flag.String("fixture", "", "Single fixture file to load (deprecated, use --fixture-dir)")
	apiURL := flag.String("api-url", "", "Base API URL for loopback server (e.g., http://127.0.0.1:8000)")
	port := flag.Int("port", 8000, "Port for loopback API server")
	flag.Parse()

	// Determine client mode and create client
	var c client.Client
	var err error

	switch {
	case *fixtureDir != "":
		// Fixture directory mode
		c, err = client.NewFixtureClient(*fixtureDir)
		if err != nil {
			fmt.Fprintf(os.Stderr, "ERROR: failed to initialize fixture client: %v\n", err)
			os.Exit(1)
		}

	case *fixture != "":
		// Legacy single fixture mode (for backward compatibility)
		dir := filepath.Dir(*fixture)
		c, err = client.NewFixtureClient(dir)
		if err != nil {
			fmt.Fprintf(os.Stderr, "ERROR: failed to initialize fixture client: %v\n", err)
			os.Exit(1)
		}

	case *apiURL != "":
		// HTTP API mode with explicit URL
		c, err = client.NewHTTPClient(*apiURL)
		if err != nil {
			fmt.Fprintf(os.Stderr, "ERROR: invalid API URL: %v\n", err)
			os.Exit(1)
		}

	default:
		// Default: construct loopback URL from port
		apiURL := fmt.Sprintf("http://127.0.0.1:%d", *port)
		c, err = client.NewHTTPClient(apiURL)
		if err != nil {
			fmt.Fprintf(os.Stderr, "ERROR: invalid API URL: %v\n", err)
			os.Exit(1)
		}
	}

	// Create and run the Bubble Tea application
	initialModel := app.New(c)
	p := tea.NewProgram(initialModel, tea.WithAltScreen())

	if _, err := p.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "ERROR: %v\n", err)
		os.Exit(1)
	}
}
