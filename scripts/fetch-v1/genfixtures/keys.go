package main

import (
	"os"
	"path/filepath"
)

// keys.go — loading the two fixture signing keys lane 0A already generated
// (tests/fixtures/fetch-v1/{test-key,other-key}/private.pem). This
// generator never writes either directory; it only reads them.

// keySourceRelPath is where the two static keys live. The ABI 2 corpus reads
// them from there too and carries byte-identical copies (copyStaticKeys), so a
// runner finds test-key/public.hex beside its own cases.json.
const keySourceRelPath = "tests/fixtures/fetch-v1"

var (
	testKey  SigningKey
	otherKey SigningKey
)

func loadKeys(root string) {
	testKey = loadSigningKey(filepath.Join(root, keySourceRelPath, "test-key", "private.pem"))
	otherKey = loadSigningKey(filepath.Join(root, keySourceRelPath, "other-key", "private.pem"))

	if len(C.TestKeys) != 1 || C.TestKeys[0].KeyID != testKey.KeyID {
		panic("genfixtures: tests/fixtures/fetch-v1/test-key/private.pem does not match " +
			"spec/fetch-v1/constants.json's test_keys[0] — regenerate one to match the other")
	}
}

// copyStaticKeys puts the v1 corpus's two static key directories into fs, for a
// corpus (the ABI 2 one) that is not the keys' home.
func copyStaticKeys(fs *FileSet, root string) {
	for _, dir := range []string{"test-key", "other-key"} {
		for _, name := range []string{"private.pem", "public.hex", "README.md"} {
			b, err := os.ReadFile(filepath.Join(root, keySourceRelPath, dir, name))
			if err != nil {
				panic("genfixtures: reading a static key file: " + err.Error())
			}
			fs.Put(dir+"/"+name, b)
		}
	}
}
