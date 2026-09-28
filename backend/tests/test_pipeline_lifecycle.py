"""A processing run owns its progressive downloader through final cleanup."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from nomusic.pipeline import downloader, processor as proc
from nomusic.pipeline.cache import JobCache
from nomusic.pipeline.downloader import DownloadCancelled, VideoMetadata


@pytest.mark.parametrize("outcome", ["complete", "queue_exhausted", "inference_error"])
def test_run_waits_for_download_cleanup_before_returning(tmp_path, monkeypatch, outcome):
    download_started = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    download_exited = threading.Event()
    run_exited = threading.Event()
    download_threads = []
    cleanup_saw_source = []
    result = {}
    url = "https://example.test/lifecycle"
    cache = JobCache(tmp_path / "cache")

    class Fetcher:
        def __init__(self, url, out_dir):
            self.url = url
            self.out_dir = out_dir

        def extract(self):
            return VideoMetadata("fixture", "Fixture", 100, "fixture", self.url)

        def close(self):
            assert not download_threads or download_exited.is_set()

        def download(self, progress_hook):
            download_threads.append(threading.current_thread())
            source = self.out_dir / "source.wav.part"
            source.write_bytes(b"downloader-owned file")
            download_started.set()
            try:
                while not release_cleanup.wait(0.005):
                    progress_hook({
                        "status": "downloading", "downloaded_bytes": 50,
                        "total_bytes": 100, "tmpfilename": str(source),
                    })
            except DownloadCancelled:
                cleanup_started.set()
                # A downloader can still use its output while unwinding its
                # network/session cleanup. The parent must keep owning it.
                assert release_cleanup.wait(5), "test did not release download cleanup"
                cleanup_saw_source.append(source.exists())
                raise
            finally:
                download_exited.set()
            return source

    def infer_batch(prepared):
        raise RuntimeError("inference failed")

    monkeypatch.setattr(proc, "SourceFetcher", Fetcher)
    processor = proc.Processor(
        engine=SimpleNamespace(infer_batch=infer_batch), cache=cache,
        chunk_seconds=10, chunk_overlap_seconds=0.5, progressive=True,
    )
    monkeypatch.setattr(
        processor, "_decode_chunk",
        lambda source, key, plan, **kwargs: proc._ChunkWork(plan, object(), 0, 0),
    )

    def on_probed(info, plans, meta):
        if outcome == "complete":
            # All chunks can exist while the complete metadata flag is still
            # false, for example after an interrupted final metadata write.
            key = cache.key(url, "fixture", ["vocals"],
                            chunk_seconds=10, chunk_overlap_seconds=0.5)
            for plan in plans:
                cache.chunk_path(key, plan.index).write_bytes(b"ready")
                cache.record_chunk(key, plan.index)

    def exhausted_provider():
        assert download_started.wait(5)
        return None

    def run():
        try:
            result["key"] = processor.run(
                url, model="fixture", keep_stems=["vocals"],
                hooks=proc.RunHooks(
                    on_probed=on_probed,
                    next_chunk_provider=(
                        None if outcome == "inference_error" else exhausted_provider
                    ),
                ),
            )
        except BaseException as exc:
            result["error"] = exc
        finally:
            run_exited.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert cleanup_started.wait(5), "processor did not cancel its downloader"
        assert not run_exited.wait(0.1), "processor returned before child cleanup finished"
        assert cache.source_dir(url).joinpath("source.wav.part").exists()
    finally:
        release_cleanup.set()
        worker.join(timeout=5)
        for thread in download_threads:
            thread.join(timeout=5)

    assert not worker.is_alive()
    assert download_exited.is_set()
    assert all(not thread.is_alive() for thread in download_threads)
    assert cleanup_saw_source == [True]
    if outcome == "inference_error":
        assert isinstance(result.get("error"), RuntimeError)
        assert str(result["error"]) == "inference failed"
    else:
        assert "error" not in result, result
        meta = cache.load_meta(result["key"])
        assert meta.complete is (outcome == "complete")
        source_dir = cache.root / "sources" / cache.url_key(url)
        assert source_dir.exists() is (outcome != "complete")


def test_executor_setup_failure_does_not_start_download(tmp_path, monkeypatch):
    download_started = threading.Event()
    release_download = threading.Event()
    download_threads = []

    class Fetcher:
        def __init__(self, url, out_dir):
            self.url = url

        def extract(self):
            return VideoMetadata("fixture", "Fixture", 100, "fixture", self.url)

        def close(self):
            pass

        def download(self, progress_hook):
            download_threads.append(threading.current_thread())
            download_started.set()
            assert release_download.wait(5)
            return tmp_path / "source.wav"

    def failed_executor(**kwargs):
        raise RuntimeError("executor setup failed")

    monkeypatch.setattr(proc, "SourceFetcher", Fetcher)
    monkeypatch.setattr(proc, "ThreadPoolExecutor", failed_executor)
    processor = proc.Processor(
        engine=object(), cache=JobCache(tmp_path / "cache"),
        chunk_seconds=10, chunk_overlap_seconds=0.5, progressive=True,
    )
    try:
        with pytest.raises(RuntimeError, match="executor setup failed"):
            processor.run("https://example.test/setup", model="fixture", keep_stems=["vocals"])
        assert not download_started.wait(0.1)
    finally:
        release_download.set()
        for thread in download_threads:
            thread.join(timeout=5)


def _install_sessions(monkeypatch, tmp_path, responses):
    """Give SourceFetcher real ownership transitions without network access."""
    import yt_dlp

    pending = iter(responses)
    sessions = []

    class Session:
        def __init__(self, opts):
            self.response = next(pending)
            self.closed = 0
            self.download_error = None
            self.downloaded = []
            sessions.append(self)

        def extract_info(self, url, download):
            if isinstance(self.response, BaseException):
                raise self.response
            return self.response

        def process_ie_result(self, info, download):
            if self.download_error is not None:
                raise self.download_error
            self.downloaded.append(info)
            (tmp_path / "source.wav").write_bytes(b"source")

        def close(self):
            self.closed += 1

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Session)
    monkeypatch.setattr(downloader, "_source_download_opts", lambda out_dir: {})
    return sessions


@pytest.mark.parametrize("response,error", [
    (RuntimeError("probe failed"), RuntimeError),
    (None, RuntimeError),
    ({"duration": None}, RuntimeError),
    ({"duration": "invalid"}, ValueError),
    (KeyboardInterrupt(), KeyboardInterrupt),
])
def test_failed_extraction_closes_session_and_fetcher_can_be_reused(
    tmp_path, monkeypatch, response, error,
):
    valid = {"id": "next", "duration": 10}
    sessions = _install_sessions(monkeypatch, tmp_path, [response, valid])
    fetcher = downloader.SourceFetcher("https://example.test/source", tmp_path)

    with pytest.raises(error):
        fetcher.extract()
    assert sessions[0].closed == 1

    assert fetcher.extract().id == "next"
    assert sessions[1].closed == 0
    assert fetcher.download().read_bytes() == b"source"
    assert sessions[1].downloaded == [valid]
    assert sessions[1].closed == 1


def test_repeated_extraction_replaces_session_and_close_is_idempotent(tmp_path, monkeypatch):
    sessions = _install_sessions(monkeypatch, tmp_path, [
        {"id": "first", "duration": 10}, {"id": "second", "duration": 20},
    ])
    fetcher = downloader.SourceFetcher("https://example.test/source", tmp_path)

    assert fetcher.extract().id == "first"
    assert fetcher.extract().id == "second"
    assert sessions[0].closed == 1
    assert sessions[1].closed == 0
    fetcher.close()
    fetcher.close()
    assert sessions[1].closed == 1


@pytest.mark.parametrize("failure", ["cached_file_removed", "interrupted", "retry"])
def test_download_failure_releases_session_before_exit_or_retry(tmp_path, monkeypatch, failure):
    source = tmp_path / "source.wav"
    if failure == "cached_file_removed":
        source.write_bytes(b"cached")
    sessions = _install_sessions(monkeypatch, tmp_path, [{"duration": 10}])
    fetcher = downloader.SourceFetcher("https://example.test/source", tmp_path)
    fetcher.extract()
    retries = []

    def retry(url, out_dir, *, progress_hook):
        assert sessions[0].closed == 1
        retries.append(url)
        return source

    monkeypatch.setattr(downloader, "download_source", retry)
    if failure == "cached_file_removed":
        source.unlink()
        with pytest.raises(FileNotFoundError):
            fetcher.download()
    elif failure == "interrupted":
        sessions[0].download_error = KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            fetcher.download()
    else:
        sessions[0].download_error = RuntimeError("session download failed")
        assert fetcher.download() == source

    assert sessions[0].closed == 1
    assert len(retries) == (1 if failure == "retry" else 0)


@pytest.mark.parametrize("failure", ["plan", "metadata_write", "probe_hook", "executor_setup"])
def test_processor_closes_prepared_session_on_setup_failure(tmp_path, monkeypatch, failure):
    sessions = _install_sessions(monkeypatch, tmp_path, [{"duration": 10}])
    cache = JobCache(tmp_path / "cache")
    processor = proc.Processor(
        engine=object(), cache=cache,
        chunk_seconds=(0.5 if failure == "plan" else 10),
        chunk_overlap_seconds=0.5, progressive=True,
    )

    def fail(*args, **kwargs):
        raise RuntimeError("preparation failed")

    if failure == "metadata_write":
        monkeypatch.setattr(cache, "save_meta", fail)
    elif failure == "executor_setup":
        monkeypatch.setattr(proc, "ThreadPoolExecutor", fail)
    hooks = proc.RunHooks(on_probed=fail if failure == "probe_hook" else None)
    with pytest.raises(ValueError if failure == "plan" else RuntimeError):
        processor.run("https://example.test/setup", model="fixture", keep_stems=["vocals"],
                      hooks=hooks)
    assert sessions[0].closed == 1
    assert sessions[0].downloaded == []
