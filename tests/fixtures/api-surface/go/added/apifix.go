// Package apifix is a fixture for scripts/api-surface.py: the base plus one
// exported function, so this must read changed.
package apifix

// Greet returns a greeting for name.
func Greet(name string) string { return prefix() + name }

// Keep is exported and identical in every variant.
func Keep() int { return 1 }

// Farewell is the addition.
func Farewell(name string) string { return "bye, " + name }

func prefix() string { return "hello, " }
