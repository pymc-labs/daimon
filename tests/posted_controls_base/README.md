# Posted control base code

These are verbatim AST-selected definitions from `pymc-labs/daimon` main at
`425f6d10fb25d5ca08a2b58d86d0a8e1d7d0bcec`. Class methods are dedented only.
`test_posted_controls_equivalence.py` executes them with the same platform
transports, store callbacks and renderers as the refactored callers.
They preserve the existing timeout race and Discord toggle behavior.
