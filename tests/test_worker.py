"""The worker: dedupe, bounded retries, progress, and three jobs draining to disk.

Real store, real worker threads, real ingest use case. Only the source is fake, so nothing here
touches the network or spawns yt-dlp.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from tunebox.config import Config
from tunebox.jobs.models import JobState
from tunebox.jobs.store import JobStore
from tunebox.jobs.worker import Worker, WorkerBusyError

from fakes import FakePlayer, FakeSource, RecordingTagWriter, video_id

VID = video_id(1)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        music_root=tmp_path / "music",
        db_path=tmp_path / "jobs.db",
        api_token="x" * 32,
        ytdlp=("false",),
        # Retries must not make the suite slow; the backoff *arithmetic* is tested separately.
        job_retry_backoff=0.0,
        job_poll_interval=0.02,
    )


@pytest.fixture
def store(config: Config) -> JobStore:
    store = JobStore(config.database_path)
    yield store
    store.close()


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


@pytest.fixture
def worker(store: JobStore, config: Config, source: FakeSource) -> Worker:
    return Worker(store=store, config=config, source=source, tag_writer=RecordingTagWriter())


def enqueue(store: JobStore, vid: str = VID, **kwargs) -> str:
    job, _created = store.enqueue(
        url=f"https://www.youtube.com/watch?v={vid}",
        video_id=vid,
        audio_format=kwargs.pop("audio_format", "m4a"),
        max_attempts=kwargs.pop("max_attempts", 3),
        **kwargs,
    )
    return job.id


def drain(worker: Worker, store: JobStore) -> None:
    """Run every claimable job to a terminal state, without threads."""
    for _ in range(50):
        job = store.claim_next()
        if job is None:
            return
        worker.run_job(job)
    raise AssertionError("queue did not drain")


class TestHappyPath:
    def test_a_job_produces_a_tagged_file_and_a_done_record(self, worker, store, config):
        job_id = enqueue(store)
        drain(worker, store)

        job = store.get(job_id)
        assert job.state is JobState.DONE
        assert job.path == str(config.music_root / f"Artist {VID}" / "Singles" / f"Track {VID}.m4a")
        assert Path(job.path).is_file()
        assert job.tags["source_id"] == VID
        assert job.progress == 100.0
        assert job.error is None

    def test_success_is_indexed_by_video_id(self, worker, store):
        """This index is what makes the next ingest of the same video free."""
        enqueue(store)
        drain(worker, store)

        record = store.find_track(VID)
        assert record is not None
        assert Path(record.path).is_file()
        assert record.artist == f"Artist {VID}"

    def test_a_per_job_format_overrides_the_default(self, worker, store, config):
        enqueue(store, audio_format="mp3")
        drain(worker, store)

        job = store.list_jobs()[0]
        assert job.path.endswith(".mp3")
        assert config.audio_format == "m4a"  # the process default is untouched

    def test_overrides_reach_both_the_tags_and_the_path(self, worker, store, config):
        enqueue(store, overrides={"artist": "Kraftwerk", "album": "Autobahn", "track_number": 1})
        drain(worker, store)

        job = store.list_jobs()[0]
        assert job.path == str(
            config.music_root / "Kraftwerk" / "Autobahn" / f"01 - Track {VID}.m4a"
        )
        assert job.tags["album_artist"] == "Kraftwerk"

    def test_progress_is_visible_while_running(self, store, config, source):
        """Recorded through the same store the API reads, so ``GET /jobs/{id}`` can show it."""
        seen: list[float | None] = []
        original = store.update_progress

        def spy(job_id: str, percent: float | None) -> None:
            original(job_id, percent)
            seen.append(store.get(job_id).progress)

        store.update_progress = spy  # type: ignore[method-assign]
        worker = Worker(
            store=store, config=config, source=source, tag_writer=RecordingTagWriter()
        )
        enqueue(store)
        drain(worker, store)

        assert seen == [50.0, 100.0]


class TestDedupeByVideoId:
    def test_a_second_job_for_the_same_video_costs_no_network_request(self, worker, store, source):
        enqueue(store)
        drain(worker, store)
        enqueue(store)
        drain(worker, store)

        second = store.list_jobs()[0]
        assert second.state is JobState.DONE
        assert second.already_present is True
        # The point of the id index: not even a metadata lookup on the repeat.
        assert source.inspect_calls == [VID]
        assert source.fetch_calls == [VID]

    def test_a_deduped_job_still_answers_with_tags_and_a_path(self, worker, store):
        enqueue(store)
        drain(worker, store)
        first = store.list_jobs()[0]

        enqueue(store)
        drain(worker, store)
        second = store.list_jobs()[0]

        assert second.path == first.path
        assert second.tags["title"] == first.tags["title"]

    def test_force_re_ingests_even_though_the_video_is_known(self, worker, store, source):
        enqueue(store)
        drain(worker, store)
        enqueue(store, force=True)
        drain(worker, store)

        assert source.fetch_calls == [VID, VID]
        assert store.list_jobs()[0].already_present is False

    def test_a_history_entry_whose_file_vanished_does_not_block_a_re_ingest(
        self, worker, store, source
    ):
        enqueue(store)
        drain(worker, store)
        Path(store.find_track(VID).path).unlink()

        enqueue(store)
        drain(worker, store)

        job = store.list_jobs()[0]
        assert job.state is JobState.DONE
        assert Path(job.path).is_file()
        assert source.fetch_calls == [VID, VID]

    def test_a_request_for_another_format_is_not_deduped_onto_the_existing_file(
        self, worker, store, source
    ):
        """Dedupe is by video id *and* container: an mp3 request must produce an mp3, not a
        `done` job pointing at the m4a that already exists."""
        enqueue(store)
        drain(worker, store)

        enqueue(store, audio_format="mp3")
        drain(worker, store)

        job = store.list_jobs()[0]
        assert job.state is JobState.DONE
        assert job.already_present is False
        assert job.path.endswith(".mp3")
        assert Path(job.path).is_file()
        assert source.fetch_calls == [VID, VID]


class TestRetries:
    def test_a_transient_failure_is_retried_and_then_succeeds(self, worker, store, source):
        source.fail_fetch_times[VID] = 1
        job_id = enqueue(store)
        drain(worker, store)

        job = store.get(job_id)
        assert job.state is JobState.DONE
        assert job.attempts == 2

    def test_retries_are_bounded_by_max_attempts(self, worker, store, source):
        source.fail_fetch_times[VID] = 99
        job_id = enqueue(store, max_attempts=3)
        drain(worker, store)

        job = store.get(job_id)
        assert job.state is JobState.FAILED
        assert job.attempts == 3
        assert len(source.fetch_calls) == 3
        assert "failed on purpose" in job.error

    def test_a_failing_metadata_lookup_is_retried_too(self, worker, store, source):
        source.fail_inspect.add(VID)
        job_id = enqueue(store, max_attempts=2)
        drain(worker, store)

        assert store.get(job_id).attempts == 2
        assert store.get(job_id).state is JobState.FAILED

    def test_a_deterministic_failure_is_not_retried(self, worker, store, source):
        """A bad override cannot succeed on the second attempt; pretending otherwise wastes a
        metadata request and delays the error the client is waiting for."""
        job_id = enqueue(store, overrides={"genre": "krautrock"})
        drain(worker, store)

        job = store.get(job_id)
        assert job.state is JobState.FAILED
        assert job.attempts == 1
        assert "unknown tag override" in job.error

    def test_a_bug_in_our_code_fails_the_job_rather_than_the_worker(self, store, config, source):
        class ExplodingWriter:
            def __call__(self, *args, **kwargs):
                raise RuntimeError("mutagen went sideways")

        worker = Worker(store=store, config=config, source=source, tag_writer=ExplodingWriter())
        job_id = enqueue(store)
        drain(worker, store)

        job = store.get(job_id)
        assert job.state is JobState.FAILED
        assert job.error.startswith("internal error: RuntimeError")

    def test_backoff_is_exponential_bounded_and_uses_the_queue_not_a_sleep(self, store, config):
        config = Config(
            music_root=config.music_root,
            db_path=config.db_path,
            api_token="x" * 32,
            job_retry_backoff=10.0,
            job_retry_backoff_max=25.0,
        )
        assert [config.retry_delay(n) for n in (1, 2, 3, 4)] == [10.0, 20.0, 25.0, 25.0]

        worker = Worker(
            store=store,
            config=config,
            source=FakeSource(fail_fetch_times={VID: 99}),
            tag_writer=RecordingTagWriter(),
        )
        job_id = enqueue(store)
        worker.run_job(store.claim_next())

        job = store.get(job_id)
        assert job.state is JobState.QUEUED
        assert job.available_at > time.time() + 5  # waiting in the queue, not in a thread
        assert store.claim_next() is None


class TestThreadedPool:
    def test_three_queued_urls_all_land(self, worker, store, config):
        """Phase 2's exit criterion, minus curl: three URLs queued, three files on disk."""
        ids = [video_id(n) for n in (1, 2, 3)]
        job_ids = [enqueue(store, vid) for vid in ids]

        worker.start()
        try:
            _wait_for(lambda: all(store.get(j).state.terminal for j in job_ids))
        finally:
            worker.stop()

        jobs = [store.get(job_id) for job_id in job_ids]
        assert [job.state for job in jobs] == [JobState.DONE] * 3
        assert all(Path(job.path).is_file() for job in jobs)
        assert sorted(p.name for p in config.music_root.glob("*/Singles/*.m4a")) == [
            f"Track {vid}.m4a" for vid in ids
        ]

    @pytest.mark.parametrize("cap", [1, 2])
    def test_no_more_than_the_configured_number_of_ingests_run_at_once(
        self, store, config, cap
    ):
        """Measures *concurrent fetches*, not thread count: the cap is a politeness limit
        towards YouTube (PLAN.md 3.1), so what matters is how many downloads overlap."""
        gate = threading.Event()
        source = _CountingSource(gate=gate)
        worker = Worker(
            store=store,
            config=replace(config, worker_concurrency=cap),
            source=source,
            tag_writer=RecordingTagWriter(),
        )
        job_ids = [enqueue(store, video_id(n)) for n in range(1, 5)]

        worker.start()
        try:
            _wait_for(lambda: source.concurrent >= cap)
            time.sleep(0.2)  # give an over-eager pool time to exceed the cap
            assert source.concurrent == cap
            gate.set()
            _wait_for(lambda: all(store.get(j).state.terminal for j in job_ids))
        finally:
            gate.set()
            worker.stop()

        assert source.peak == cap
        assert [store.get(j).state for j in job_ids] == [JobState.DONE] * 4

    def test_a_second_worker_on_the_same_queue_is_refused(self, store, config, source):
        """Two `tunebox serve` processes would each requeue the other's in-flight jobs and then
        download the same video twice."""
        first = Worker(store=store, config=config, source=source, tag_writer=RecordingTagWriter())
        second = Worker(store=store, config=config, source=source, tag_writer=RecordingTagWriter())
        first.start()
        try:
            with pytest.raises(WorkerBusyError, match="already using"):
                second.start()
        finally:
            first.stop()

        # Once the first one is gone the lock is free again.
        second.start()
        second.stop()

    def test_stopping_an_idle_worker_joins_its_threads(self, worker):
        worker.start()
        worker.stop()
        assert worker.running is False

    def test_a_job_interrupted_by_a_restart_is_picked_up_again(self, worker, store):
        job_id = enqueue(store)
        store.claim_next()  # pretend the previous process died here

        worker.start()
        try:
            _wait_for(lambda: store.get(job_id).state is JobState.DONE)
        finally:
            worker.stop()

        assert store.get(job_id).attempts == 2


class TestStuckJobs:
    def test_a_failure_while_recording_the_outcome_does_not_kill_the_thread(
        self, store, config, source
    ):
        """The worst version of this bug: the job stays `running` forever *and* every job behind
        it in the queue is never served, while the API keeps answering 200."""
        worker = Worker(
            store=store, config=config, source=source, tag_writer=RecordingTagWriter()
        )
        first = enqueue(store, video_id(1))
        second = enqueue(store, video_id(2))

        calls = {"n": 0}
        real_mark_done = store.mark_done

        def flaky_mark_done(job_id, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            real_mark_done(job_id, **kwargs)

        store.mark_done = flaky_mark_done  # type: ignore[method-assign]
        worker.start()
        try:
            _wait_for(lambda: store.get(second).state is JobState.DONE)
            # The thread that hit the error is still serving the queue, not dead.
            assert worker.running is True
        finally:
            worker.stop()

        assert store.get(second).state is JobState.DONE  # the queue kept moving
        assert store.get(first).state is not JobState.RUNNING  # and nothing is wedged

    def test_a_job_abandoned_while_running_is_reclaimed(self, store, config):
        """Belt and braces behind the adapter's own timeouts: nothing may sit in `running`
        forever."""
        job_id = enqueue(store)
        store.claim_next()
        with store._writing() as conn:  # noqa: SLF001 - simulating a start that never finished
            conn.execute("UPDATE jobs SET started_at = 1000.0 WHERE id = ?", (job_id,))

        reclaimed = store.reclaim_stale(started_before=time.time() - config.job_timeout)
        assert reclaimed == [job_id]
        assert store.get(job_id).state is JobState.QUEUED
        assert "abandoned" in store.get(job_id).error

    def test_a_live_job_is_never_reclaimed_from_under_its_own_thread(self, store, config):
        """Reclaiming a running download would run it twice."""
        job_id = enqueue(store)
        store.claim_next()
        with store._writing() as conn:  # noqa: SLF001
            conn.execute("UPDATE jobs SET started_at = 1000.0 WHERE id = ?", (job_id,))

        reclaimed = store.reclaim_stale(
            started_before=time.time() - config.job_timeout, exclude=[job_id]
        )
        assert reclaimed == []
        assert store.get(job_id).state is JobState.RUNNING

    def test_an_abandoned_job_out_of_attempts_is_failed_not_requeued(self, store, config):
        job_id = enqueue(store, max_attempts=1)
        store.claim_next()
        with store._writing() as conn:  # noqa: SLF001
            conn.execute("UPDATE jobs SET started_at = 1000.0 WHERE id = ?", (job_id,))

        store.reclaim_stale(started_before=time.time() - config.job_timeout)
        assert store.get(job_id).state is JobState.FAILED

    def test_the_job_timeout_is_longer_than_the_download_timeout(self, config):
        """Otherwise a legitimately slow download gets reclaimed while it is still working."""
        assert config.job_timeout > config.fetch_timeout + config.inspect_timeout


class _CountingSource(FakeSource):
    """Counts how many fetches are in flight at once."""

    def __init__(self, gate: threading.Event) -> None:
        super().__init__(gate=gate)
        self.concurrent = 0
        self.peak = 0
        self._counter_lock = threading.Lock()

    def fetch(self, *args, **kwargs):
        with self._counter_lock:
            self.concurrent += 1
            self.peak = max(self.peak, self.concurrent)
        try:
            return super().fetch(*args, **kwargs)
        finally:
            with self._counter_lock:
                self.concurrent -= 1


def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within the timeout")


class TestRescanHook:
    """PLAN.md Phase 3: *"library rescan hook after ingest"*.

    The hook is the smallest piece of Phase 3 and the one with the sharpest rule attached: it can
    never turn a finished download into a failed job. A retry after a "failure" caused by a stopped
    OwnTone would re-download the file for nothing.
    """

    def _worker(self, store, config, source, player, **overrides) -> Worker:
        return Worker(
            store=store,
            config=replace(config, **overrides) if overrides else config,
            source=source,
            tag_writer=RecordingTagWriter(),
            player=player,
        )

    def test_a_new_file_triggers_exactly_one_rescan(self, store, config, source):
        player = FakePlayer()
        worker = self._worker(store, config, source, player)
        job_id = enqueue(store)

        drain(worker, store)

        assert store.get(job_id).state is JobState.DONE
        assert player.rescans == 1

    def test_a_deduped_job_does_not_rescan(self, store, config, source):
        """Nothing changed on disk, so a rescan could only find what the daemon already knows."""
        player = FakePlayer()
        worker = self._worker(store, config, source, player)
        enqueue(store)
        drain(worker, store)
        assert player.rescans == 1

        enqueue(store)
        drain(worker, store)
        assert player.rescans == 1

    def test_a_dead_player_is_a_warning_on_a_done_job_not_a_failure(self, store, config, source):
        player = FakePlayer(unavailable=True)
        worker = self._worker(store, config, source, player)
        job_id = enqueue(store)

        drain(worker, store)

        job = store.get(job_id)
        assert job.state is JobState.DONE, "a stopped OwnTone must not cost us the download"
        assert job.attempts == 1, "and must not trigger a retry that downloads it again"
        assert any("library rescan failed" in warning for warning in job.warnings)
        assert Path(job.path).is_file()

    def test_the_warning_reaches_the_job_record_not_just_the_log(self, store, config, source):
        """The rescan runs *before* ``mark_done``; a warning written afterwards would never reach
        ``GET /jobs/{id}``."""
        player = FakePlayer(rescan_error=RuntimeError("owntone exploded"))
        worker = self._worker(store, config, source, player)
        job_id = enqueue(store)

        drain(worker, store)

        job = store.get(job_id)
        assert job.state is JobState.DONE
        assert any("owntone exploded" in warning for warning in job.warnings)

    def test_a_failed_ingest_does_not_rescan(self, store, config):
        player = FakePlayer()
        source = FakeSource(fail_fetch_times={VID: 99})
        worker = self._worker(store, config, source, player, job_max_attempts=1)
        job_id = enqueue(store, max_attempts=1)

        drain(worker, store)

        assert store.get(job_id).state is JobState.FAILED
        assert player.rescans == 0

    def test_the_hook_can_be_switched_off(self, store, config, source):
        player = FakePlayer()
        worker = self._worker(store, config, source, player, rescan_after_ingest=False)
        enqueue(store)

        drain(worker, store)

        assert player.rescans == 0

    def test_a_worker_with_no_player_behaves_exactly_as_it_did_in_phase_2(
        self, store, config, source
    ):
        """Ingest must keep working on a host with no player daemon installed at all."""
        worker = Worker(
            store=store, config=config, source=source, tag_writer=RecordingTagWriter()
        )
        job_id = enqueue(store)

        drain(worker, store)

        job = store.get(job_id)
        assert job.state is JobState.DONE
        assert job.warnings == ()
