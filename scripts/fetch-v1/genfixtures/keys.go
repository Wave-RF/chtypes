package main

import "path/filepath"

// keys.go — loading the two fixture signing keys lane 0A already generated
// (tests/fixtures/fetch-v1/{test-key,other-key}/private.pem). This
// generator never writes either directory; it only reads them.

var (
	testKey  SigningKey
	otherKey SigningKey
)

func loadKeys(root string) {
	testKey = loadSigningKey(filepath.Join(root, fixturesRelPath, "test-key", "private.pem"))
	otherKey = loadSigningKey(filepath.Join(root, fixturesRelPath, "other-key", "private.pem"))

	if len(C.TestKeys) != 1 || C.TestKeys[0].KeyID != testKey.KeyID {
		panic("genfixtures: tests/fixtures/fetch-v1/test-key/private.pem does not match " +
			"spec/fetch-v1/constants.json's test_keys[0] — regenerate one to match the other")
	}
}
