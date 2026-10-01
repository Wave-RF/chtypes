// Package apifix is a fixture for scripts/api-surface.py: Greet takes one
// more parameter than in the base, so this must read changed.
package apifix

// Greet returns a greeting for name.
func Greet(name string, excited bool) string {
	if excited {
		return prefix() + name + "!"
	}
	return prefix() + name
}

// Keep is exported and identical in every variant.
func Keep() int { return 1 }

func prefix() string { return "hello, " }
