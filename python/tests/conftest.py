"""Shared test setup. The v1 suites that need a native library run against the
stub libraries scripts/abi-v1/build-stubs.sh builds (`$CHTYPES_ABI2_STUBS`) and
skip, loudly and by name, without them: a green run that never loaded a library
would be worse than no run. Pure-Python suites (decoders, errors, settings,
setup) need none and always run.
"""
