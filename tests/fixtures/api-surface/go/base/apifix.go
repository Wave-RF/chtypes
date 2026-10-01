// Package apifix is a fixture for scripts/api-surface.py: the base API every
// other variant is compared against.
package apifix

// Greet returns a greeting for name.
func Greet(name string) string { return prefix() + name }

// Keep is exported and identical in every variant.
func Keep() int { return 1 }

func prefix() string { return "hello, " }
