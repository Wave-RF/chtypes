// Package apifix is a fixture for scripts/api-surface.py: only unexported
// code and doc comments differ from the base, so this must read unchanged.
package apifix

// Greet returns a friendly greeting for name, built by a renamed helper.
func Greet(name string) string { return salutation() + name }

// Keep is exported and identical in every variant.
func Keep() int { return 1 }

func salutation() string { return "hi, " }

type unexportedState struct{ calls int }

func (s *unexportedState) bump() { s.calls++ }
