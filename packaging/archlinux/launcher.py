"""Entry point for the packaged corpus-graph: put the bundled libraries first on sys.path.

Run as `python -s -E -X utf8 launcher.py [--lib DIR] ARGS...`; /usr/bin/corpus-graph does this.
"""
import os
import sys

args = sys.argv[1:]
if args[:1] == ["--lib"]:
    lib, args = args[1], args[2:]
else:
    lib = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
sys.path.insert(0, lib)
sys.argv = ["corpus-graph", *args]

from corpus_graph.cli import main  # noqa: E402

sys.exit(main())
