# corpus-graph. `make sample` runs the whole pipeline on the bundled synthetic articles using
# recorded LLM responses (no tokens spent) and opens the viewer.
CG := uv run corpus-graph
SAMPLE := --root sample
SAMPLE_CHUNKS := --chunk-tokens 700   # small chunks so the 3 short articles exercise multi-chunk merging

.PHONY: sample sample-build sample-live test serve clean-sample install

install:
	uv sync

sample-build:
	$(CG) $(SAMPLE) init
	$(CG) $(SAMPLE) ingest
	$(CG) $(SAMPLE) extract --backend replay --fixtures sample/fixtures $(SAMPLE_CHUNKS)
	$(CG) $(SAMPLE) resolve
	$(CG) $(SAMPLE) build
	$(CG) $(SAMPLE) report
	$(CG) $(SAMPLE) serve --write-only

sample: sample-build
	$(CG) $(SAMPLE) serve

# Re-extract the sample with the real model (costs ~$0.30 at list price) and refresh the fixtures.
sample-live:
	$(CG) $(SAMPLE) ingest
	$(CG) $(SAMPLE) extract --backend claude-cli $(SAMPLE_CHUNKS) --force --record sample/fixtures
	$(CG) $(SAMPLE) resolve
	$(CG) $(SAMPLE) build
	$(CG) $(SAMPLE) report
	$(CG) $(SAMPLE) serve --write-only

test:
	uv run pytest -q

serve:
	$(CG) serve

clean-sample:
	rm -rf sample/work sample/out
