import shutil
from pathlib import Path

import pytest

from corpus_graph import build, extract, ingest, report, resolve, serve
from corpus_graph.canon import init_canon
from corpus_graph.util import Workspace

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample"
SAMPLE_CHUNK_TOKENS = 700


def copy_sample(dest: Path) -> Workspace:
    (dest / "input").mkdir(parents=True)
    for f in (SAMPLE / "input").iterdir():
        shutil.copy(f, dest / "input" / f.name)
    init_canon(dest / "canon")
    return Workspace.from_args(root=str(dest))


def run_pipeline(ws: Workspace) -> None:
    ingest.run(ws, quiet=True)
    extract.run(ws, backend="replay", fixtures=SAMPLE / "fixtures", chunk_tokens=SAMPLE_CHUNK_TOKENS)
    resolve.run(ws, quiet=True)
    build.run(ws, quiet=True)
    report.run(ws, quiet=True)
    serve.write_viewer(ws)


@pytest.fixture(scope="session")
def sample_ws(tmp_path_factory) -> Workspace:
    """The bundled sample, run end to end with recorded LLM responses (read-only for tests)."""
    ws = copy_sample(tmp_path_factory.mktemp("sample"))
    run_pipeline(ws)
    return ws


@pytest.fixture
def fresh_sample(tmp_path) -> Workspace:
    """A private copy of the sample pipeline for tests that change state."""
    ws = copy_sample(tmp_path / "ws")
    run_pipeline(ws)
    return ws
