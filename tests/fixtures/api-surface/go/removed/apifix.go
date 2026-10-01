// Package apifix is a fixture for scripts/api-surface.py: the base without
// Greet, so this must read changed.
package apifix

// Keep is exported and identical in every variant.
func Keep() int { return 1 }

func prefix() string { return "hello, " }
