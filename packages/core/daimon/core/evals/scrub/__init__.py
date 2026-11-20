"""Scrubbing: real conversations become cases that hold only consistent fakes.

`entities` reads the known-names dictionary, `fakes` keeps the real-to-fake map by keyed hash,
`detect` finds what to replace, `engine` applies it to text and documents, `files` and `images`
rebuild blobs, `sweep` searches the result for leftovers and `bundle` does all of it to a whole
bundle offline. Only the `evals` extra's libraries are heavy; each is imported on first use.
"""
