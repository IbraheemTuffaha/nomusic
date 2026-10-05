"""Synchronous acquisition observes cancellation without retrying or inference."""

from types import SimpleNamespace

import pytest

from nomusic.jobs import WorkerAbandoned
from nomusic.pipeline import downloader, processor as pipeline
from nomusic.pipeline.cache import CacheMeta, JobCache


@pytest.fixture
def acquisition(tmp_path, monkeypatch):
    import yt_dlp

    state = SimpleNamespace(stopped=False, sessions=[], downloads=[], stop_during_probe=False)
    url = "https://example.test/synchronous-download"
    cache = JobCache(tmp_path / "cache")

    def abort():
        if state.stopped:
            raise WorkerAbandoned("test stop")

    class Session:
        def __init__(self, opts):
            self.hooks = list(opts.get("progress_hooks", []))
            self.closed = 0
            state.sessions.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def extract_info(self, url, download):
            assert not download
            if state.stop_during_probe:
                state.stopped = True
            return {"id": "fixture", "title": "Fixture", "duration": 10}

        def add_progress_hook(self, hook):
            self.hooks.append(hook)

        def process_ie_result(self, info, download):
            assert download
            self._transfer("same-session")

        def download(self, urls):
            self._transfer("direct")

        def _transfer(self, mode):
            state.downloads.append(mode)
            state.stopped = True
            for hook in self.hooks:
                hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 100})
            pytest.fail("download ignored cancellation")

        def close(self):
            self.closed += 1

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Session)
    monkeypatch.setattr(downloader, "_source_download_opts", lambda _: {})
    processor = pipeline.Processor(
        engine=object(), cache=cache, chunk_seconds=10,
        chunk_overlap_seconds=0.5, progressive=False,
    )

    def no_decode(*args, **kwargs):
        pytest.fail("cancelled acquisition reached decode/inference")

    monkeypatch.setattr(processor, "_decode_chunk", no_decode)
    state.url, state.cache, state.processor, state.abort = url, cache, processor, abort
    yield state
    cache.close()


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("with_ui", [False, True])
def test_full_download_cancels_and_closes_real_fetcher_without_retry(acquisition, resume, with_ui):
    state = acquisition
    if resume:
        key = state.cache.key(state.url, "fixture", ["vocals"],
                              chunk_seconds=10, chunk_overlap_seconds=0.5)
        state.cache.save_meta(key, CacheMeta(
            url=state.url, model="fixture", keep_stems=["vocals"],
            duration_seconds=10, chunk_seconds=10, chunk_overlap_seconds=0.5,
            total_chunks=len(pipeline.plan_chunks(10, 10, 0.5)),
        ))
    ui_updates = []
    with pytest.raises(WorkerAbandoned, match="test stop"):
        state.processor.run(state.url, model="fixture", keep_stems=["vocals"],
                            hooks=pipeline.RunHooks(
                                abort_check=state.abort,
                                on_download_progress=ui_updates.append if with_ui else None,
                            ))
    assert state.downloads == ["direct" if resume else "same-session"]
    assert len(state.sessions) == 1  # cancellation must not open a retry session
    assert state.sessions[0].closed == 1
    assert ui_updates == []


@pytest.mark.parametrize("stage", ["before_run", "during_probe", "after_probe"])
@pytest.mark.parametrize("progressive", [False, True])
def test_stopped_run_never_starts_new_acquisition(acquisition, stage, progressive):
    state = acquisition
    state.processor.progressive = progressive
    state.stopped = stage == "before_run"
    state.stop_during_probe = stage == "during_probe"

    def on_probed(*_args):
        if stage == "after_probe":
            state.stopped = True

    with pytest.raises(WorkerAbandoned, match="test stop"):
        state.processor.run(state.url, model="fixture", keep_stems=["vocals"],
                            hooks=pipeline.RunHooks(abort_check=state.abort, on_probed=on_probed))
    assert state.downloads == []
    assert len(state.sessions) == (0 if stage == "before_run" else 1)
    assert all(session.closed == 1 for session in state.sessions)
