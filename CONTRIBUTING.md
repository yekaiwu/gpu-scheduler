# Contributing

## Running the tests

```sh
python3.12 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest -q            # default suite, no live kernel needed
.venv/bin/python -m pytest -m live       # Apple silicon only: real ctypes probes, real processes
```

Socket paths are limited to 104 bytes on macOS. Tests that bind sockets use short
directories under `/tmp` (see the `short_tmp` fixture), never `tmp_path`.

## Ground rules

Every change is reviewed against the safety invariants in
[docs/implementation/README.md](docs/implementation/README.md#ground-rules-non-negotiable):
never signal a process the daemon did not spawn, never signal atomic or spilled jobs on
policy, commit before you speak, one thread, pure policy, stdlib only at runtime, no
machine-specific constants, bytes internally, fail closed, and observe mode changes nothing.

Open questions and judgment calls go in `docs/implementation/QUESTIONS.md`.
