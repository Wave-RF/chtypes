// parity-surface-go.go — one Go package's exported surface, as the compiler
// exported it, for scripts/parity-surface.py. Read that file's header first:
// it owns the comparison, this file only lists.
//
//	go run scripts/parity-surface-go.go <module dir> <import path>
//
// run with the build's tags in GOFLAGS (the caller sets GOWORK=off,
// GOTOOLCHAIN=local and GOFLAGS=-tags=…, the same environment
// scripts/api-surface.py gives apidiff).
//
// WHERE THE SURFACE COMES FROM. `go list -export -deps` compiles the package
// and names the export data file the compiler wrote for it and for each of its
// dependencies. go/importer's "gc" importer reads those files, so the
// types.Package below is the compiler's own record of what the package
// exports: the same export data apidiff (scripts/api-surface.py) reads through
// golang.org/x/tools/go/packages, read here through the standard library only,
// because apidiff prints differences and never a listing. Nothing from the
// package runs: it is compiled, never executed. Source text is never read.
//
// WHAT IS LISTED, one JSON object per name, sorted:
//
//   - every exported package-level object: {"name": "Setup", "kind": "func"};
//     a constant or variable also carries its type ("type": "chtypes.Status");
//   - for every exported named type T: each exported method declared on T or
//     *T ("T.Close", kind "method"), each exported struct field ("T.Version",
//     kind "field"), and each exported method of an interface type ("T.M",
//     kind "method");
//   - a field or method promoted from an embedded type is listed on T only
//     when the embedded type is not itself exported: an exported embedded
//     type (Go's SchemaError embeds CallError) lists its own members, once.
//
// A type alias lists the members of the type it names, under the alias's own
// name, because that is how a caller reaches them.
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"go/importer"
	"go/token"
	"go/types"
	"io"
	"os"
	"os/exec"
	"sort"
	"strings"
)

type entry struct {
	Name string `json:"name"`
	Kind string `json:"kind"`
	Type string `json:"type,omitempty"`
}

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: go run parity-surface-go.go <module dir> <import path>")
		os.Exit(2)
	}
	dir, path := os.Args[1], os.Args[2]
	exports, err := exportFiles(dir, path)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	lookup := func(p string) (io.ReadCloser, error) {
		f, ok := exports[p]
		if !ok || f == "" {
			return nil, fmt.Errorf("go list -export named no export data for %s", p)
		}
		return os.Open(f)
	}
	pkg, err := importer.ForCompiler(token.NewFileSet(), "gc", lookup).Import(path)
	if err != nil {
		fmt.Fprintln(os.Stderr, "importing", path, "from its export data:", err)
		os.Exit(1)
	}
	out := list(pkg)
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", " ")
	if err := enc.Encode(out); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

// exportFiles maps each import path of the package and its dependencies to the
// export data file `go list -export` names for it.
func exportFiles(dir, path string) (map[string]string, error) {
	cmd := exec.Command("go", "list", "-export", "-deps", "-f", "{{.ImportPath}}\t{{.Export}}", path)
	cmd.Dir = dir
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	raw, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("go list -export -deps %s (in %s): %v\n%s", path, dir, err, stderr.String())
	}
	files := map[string]string{}
	sc := bufio.NewScanner(bytes.NewReader(raw))
	for sc.Scan() {
		ip, file, ok := strings.Cut(sc.Text(), "\t")
		if ok {
			files[ip] = file
		}
	}
	if files[path] == "" {
		return nil, fmt.Errorf("go list -export named no export data for %s", path)
	}
	return files, nil
}

func list(pkg *types.Package) []entry {
	var out []entry
	qual := types.RelativeTo(pkg)
	scope := pkg.Scope()
	for _, name := range scope.Names() {
		obj := scope.Lookup(name)
		if !obj.Exported() {
			continue
		}
		switch o := obj.(type) {
		case *types.Func:
			out = append(out, entry{Name: name, Kind: "func"})
		case *types.Const:
			out = append(out, entry{Name: name, Kind: "const", Type: types.TypeString(o.Type(), qual)})
		case *types.Var:
			out = append(out, entry{Name: name, Kind: "var", Type: types.TypeString(o.Type(), qual)})
		case *types.TypeName:
			kind := "type"
			if o.IsAlias() {
				kind = "alias"
			}
			out = append(out, entry{Name: name, Kind: kind})
			out = append(out, members(name, o.Type())...)
		}
	}
	return out
}

// members lists T's exported methods and fields, as the header says.
func members(owner string, t types.Type) []entry {
	var out []entry
	seen := map[string]bool{}
	add := func(name, kind string) {
		if !seen[name] {
			seen[name] = true
			out = append(out, entry{Name: owner + "." + name, Kind: kind})
		}
	}
	t = types.Unalias(t)
	if iface, ok := t.Underlying().(*types.Interface); ok {
		for i := 0; i < iface.NumMethods(); i++ {
			if m := iface.Method(i); m.Exported() {
				add(m.Name(), "method")
			}
		}
		return out
	}
	// The method set of *T holds every method of T and *T, promoted ones
	// included; a promoted one is kept only when every embedded type on its
	// path is unexported.
	mset := types.NewMethodSet(types.NewPointer(t))
	for i := 0; i < mset.Len(); i++ {
		sel := mset.At(i)
		if sel.Obj().Exported() && viaUnexported(t, sel.Index()) {
			add(sel.Obj().Name(), "method")
		}
	}
	if st, ok := t.Underlying().(*types.Struct); ok {
		fields(st, add, 0)
	}
	return out
}

// fields adds st's exported fields, and those promoted through an embedded
// field whose type is not exported.
func fields(st *types.Struct, add func(name, kind string), depth int) {
	if depth > 8 {
		return
	}
	for i := 0; i < st.NumFields(); i++ {
		f := st.Field(i)
		if f.Exported() {
			add(f.Name(), "field")
			continue
		}
		if f.Embedded() {
			if inner, ok := deref(f.Type()).Underlying().(*types.Struct); ok {
				fields(inner, add, depth+1)
			}
		}
	}
}

// viaUnexported reports whether the selection path index (embedded fields,
// then the method) passes only through unexported embedded fields, so the
// method is T's own as far as a caller can see.
func viaUnexported(t types.Type, index []int) bool {
	cur := t
	for _, i := range index[:len(index)-1] {
		st, ok := deref(cur).Underlying().(*types.Struct)
		if !ok {
			return false
		}
		f := st.Field(i)
		if f.Exported() {
			return false
		}
		cur = f.Type()
	}
	return true
}

func deref(t types.Type) types.Type {
	if p, ok := types.Unalias(t).(*types.Pointer); ok {
		return p.Elem()
	}
	return types.Unalias(t)
}
