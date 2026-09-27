"""The SQLite job queue. Real database on a temporary path; no network, no yt-dlp."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from tunebox.jobs.models import JobState
from tunebox.jobs.store import JobStore


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    store = JobStore(tmp_path / "state" / "jobs.db")
    yield store
    store.close()


def _enqueue(store: JobStore, vid: str = "vid00000001", **kwargs) -> str:
    job, _created = store.enqueue(
        url=f"https://www.youtube.com/watch?v={vid}",
        video_id=vid,
        audio_format="m4a",
        **kwargs,
    )
    return job.id


class TestSchema:
    def test_database_file_and_parent_are_created(self, tmp_path: Path):
        store = JobStore(tmp_path / "nested" / "dir" / "jobs.db")
        assert (tmp_path / "nested" / "dir" / "jobs.db").exists()
        store.close()

    def test_opening_an_existing_database_keeps_its_jobs(self, tmp_path: Path):
        path = tmp_path / "jobs.db"
        first = JobStore(path)
        job_id = _enqueue(first)
        first.close()

        second = JobStore(path)
        assert second.get(job_id) is not None
        second.close()


class TestEnqueue:
    def test_a_new_job_starts_queued_and_unattempted(self, store: JobStore):
        job, created = store.enqueue(
            url="https://x/", video_id="vid00000001", audio_format="m4a"
        )
        assert created is True
        assert job.state is JobState.QUEUED
        assert job.attempts == 0
        assert store.get(job.id) == job

    def test_ids_are_unguessable_rather_than_sequential(self, store: JobStore):
        ids = {_enqueue(store, f"vid{n:08d}") for n in range(5)}
        assert len(ids) == 5
        assert all(len(job_id) >= 16 for job_id in ids)

    def test_overrides_and_flags_survive_a_round_trip(self, store: JobStore):
        job, _ = store.enqueue(
            url="https://x/",
            video_id="vid00000001",
            audio_format="mp3",
            overrides={"artist": "陳奕迅", "track_number": 3},
            force=True,
            max_attempts=5,
        )
        stored = store.get(job.id)
        assert stored.overrides == {"artist": "陳奕迅", "track_number": 3}
        assert stored.force is True
        assert stored.audio_format == "mp3"
        assert stored.max_attempts == 5

    def test_unknown_job_id_is_none_not_an_error(self, store: JobStore):
        assert store.get("deadbeefdeadbeef") is None

    def test_dedupe_at_enqueue_returns_the_existing_job_instead_of_a_second_one(
        self, store: JobStore
    ):
        first = _enqueue(store, "vid00000001")
        job, created = store.enqueue(
            url="https://x/", video_id="vid00000001", audio_format="m4a", dedupe_active=True
        )
        assert created is False
        assert job.id == first
        assert len(store.list_jobs()) == 1

    def test_dedupe_at_enqueue_ignores_finished_jobs(self, store: JobStore):
        first = _enqueue(store, "vid00000001")
        store.claim_next()
        store.mark_done(first, path="/x.m4a", tags=None, already_present=False)

        _, created = store.enqueue(
            url="https://x/", video_id="vid00000001", audio_format="m4a", dedupe_active=True
        )
        assert created is True

    def test_simultaneous_enqueues_of_one_video_create_exactly_one_job(self, store: JobStore):
        """The check and the insert share a transaction, so two taps cannot both win."""
        created_flags: list[bool] = []
        lock = threading.Lock()
        start = threading.Barrier(6)

        def attempt() -> None:
            start.wait(timeout=10)
            _job, created = store.enqueue(
                url="https://x/", video_id="vid00000001", audio_format="m4a", dedupe_active=True
            )
            with lock:
                created_flags.append(created)

        threads = [threading.Thread(target=attempt) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert created_flags.count(True) == 1
        assert len(store.list_jobs()) == 1

    def test_dedupe_holds_across_separate_connections(self, tmp_path: Path):
        """Same reason as the claim test: with one store per thread, only the transaction stops
        two simultaneous requests from both inserting."""
        path = tmp_path / "shared.db"
        stores = [JobStore(path) for _ in range(4)]
        try:
            created: list[bool] = []
            lock = threading.Lock()
            ready = threading.Barrier(len(stores))

            def attempt(store: JobStore) -> None:
                ready.wait(timeout=10)
                _job, was_created = store.enqueue(
                    url="https://x/",
                    video_id="vid00000001",
                    audio_format="m4a",
                    dedupe_active=True,
                )
                with lock:
                    created.append(was_created)

            threads = [threading.Thread(target=attempt, args=(s,)) for s in stores]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)

            assert created.count(True) == 1
            assert len(stores[0].list_jobs()) == 1
        finally:
            for store in stores:
                store.close()

    def test_a_different_format_is_a_different_request_not_a_duplicate(self, store: JobStore):
        """Deduping an mp3 request onto a queued m4a job would silently never produce the mp3."""
        _enqueue(store, "vid00000001")
        job, created = store.enqueue(
            url="https://x/", video_id="vid00000001", audio_format="mp3", dedupe_active=True
        )
        assert created is True
        assert job.audio_format == "mp3"

    def test_different_overrides_are_a_different_request(self, store: JobStore):
        """Otherwise a second POST carrying --artist gets a 200 and its correction is dropped."""
        store.enqueue(
            url="https://x/",
            video_id="vid00000001",
            audio_format="m4a",
            overrides={"artist": "Kraftwerk"},
            dedupe_active=True,
        )
        _job, created = store.enqueue(
            url="https://x/",
            video_id="vid00000001",
            audio_format="m4a",
            overrides={"artist": "Kraftwerk", "album": "Autobahn"},
            dedupe_active=True,
        )
        assert created is True

    def test_an_identical_request_is_deduped_even_among_several_jobs(self, store: JobStore):
        first = _enqueue(store, "vid00000001", overrides={"artist": "A"})
        store.enqueue(
            url="https://x/", video_id="vid00000001", audio_format="mp3", dedupe_active=True
        )
        job, created = store.enqueue(
            url="https://x/",
            video_id="vid00000001",
            audio_format="m4a",
            overrides={"artist": "A"},
            dedupe_active=True,
        )
        assert created is False
        assert job.id == first


class TestClaim:
    def test_claiming_marks_running_and_counts_the_attempt(self, store: JobStore):
        job_id = _enqueue(store)
        claimed = store.claim_next()
        assert claimed.id == job_id
        assert claimed.state is JobState.RUNNING
        assert claimed.attempts == 1
        assert store.get(job_id).started_at is not None

    def test_an_empty_queue_claims_nothing(self, store: JobStore):
        assert store.claim_next() is None

    def test_a_job_is_claimed_exactly_once(self, store: JobStore):
        """The property the whole BEGIN IMMEDIATE dance exists for."""
        for n in range(20):
            _enqueue(store, f"vid{n:08d}")

        claimed: list[str] = []
        lock = threading.Lock()

        def drain() -> None:
            while True:
                job = store.claim_next()
                if job is None:
                    return
                with lock:
                    claimed.append(job.id)

        threads = [threading.Thread(target=drain) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert len(claimed) == 20
        assert len(set(claimed)) == 20

    def test_a_job_is_claimed_once_even_across_separate_connections(self, tmp_path: Path):
        """Two stores mean two connections and two in-process locks, so this exercises the
        database-level guarantee rather than a Python lock.

        Note what carries the weight: the conditional ``UPDATE ... WHERE state = 'queued'`` and
        the rowcount check. A thread that loses the race updates nothing and reports no job, so
        double-claiming is impossible even before the transaction is considered; ``BEGIN
        IMMEDIATE`` is what makes the *read* and that write one step, which matters for the
        dedupe query in ``enqueue`` (see the next test, which does fail without it).
        """
        path = tmp_path / "shared.db"
        stores = [JobStore(path) for _ in range(3)]
        try:
            for n in range(15):
                _enqueue(stores[0], f"vid{n:08d}")

            claimed: list[str] = []
            lock = threading.Lock()
            ready = threading.Barrier(len(stores))

            def drain(store: JobStore) -> None:
                ready.wait(timeout=10)
                while True:
                    job = store.claim_next()
                    if job is None:
                        return
                    with lock:
                        claimed.append(job.id)

            threads = [threading.Thread(target=drain, args=(s,)) for s in stores]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)

            assert len(claimed) == 15
            assert len(set(claimed)) == 15
        finally:
            for store in stores:
                store.close()

    def test_jobs_are_claimed_oldest_first(self, store: JobStore):
        first = _enqueue(store, "vid00000001", now=1000.0)
        second = _enqueue(store, "vid00000002", now=2000.0)
        assert store.claim_next().id == first
        assert store.claim_next().id == second

    def test_a_job_scheduled_for_later_is_not_claimed_yet(self, store: JobStore):
        job_id = _enqueue(store)
        store.claim_next()
        store.reschedule(job_id, error="boom", available_at=time.time() + 300)

        assert store.claim_next() is None
        assert store.get(job_id).state is JobState.QUEUED

    def test_a_backoff_that_has_elapsed_is_claimable(self, store: JobStore):
        job_id = _enqueue(store)
        store.claim_next()
        store.reschedule(job_id, error="boom", available_at=time.time() - 1)
        assert store.claim_next().id == job_id


class TestTerminalStates:
    def test_mark_done_records_the_path_tags_and_warnings(self, store: JobStore):
        job_id = _enqueue(store)
        store.claim_next()
        store.mark_done(
            job_id,
            path="/music/A/Singles/T.m4a",
            tags={"title": "T", "artist": "A"},
            already_present=False,
            warnings=["tagging failed: no writer"],
        )
        job = store.get(job_id)
        assert job.state is JobState.DONE
        assert job.path == "/music/A/Singles/T.m4a"
        assert job.tags == {"title": "T", "artist": "A"}
        assert job.warnings == ("tagging failed: no writer",)
        assert job.progress == 100.0
        assert job.finished_at is not None

    def test_mark_failed_keeps_the_error(self, store: JobStore):
        job_id = _enqueue(store)
        store.claim_next()
        store.mark_failed(job_id, "SourceError: 403")
        job = store.get(job_id)
        assert job.state is JobState.FAILED
        assert job.error == "SourceError: 403"

    def test_progress_is_only_recorded_while_running(self, store: JobStore):
        job_id = _enqueue(store)
        store.update_progress(job_id, 50.0)
        assert store.get(job_id).progress is None  # still queued

        store.claim_next()
        store.update_progress(job_id, 50.0)
        assert store.get(job_id).progress == 50.0

    def test_cancel_applies_to_queued_jobs_only(self, store: JobStore):
        queued = _enqueue(store, "vid00000001")
        assert store.cancel(queued) is True
        assert store.get(queued).state is JobState.FAILED
        assert store.get(queued).error == "cancelled"

        running = _enqueue(store, "vid00000002")
        store.claim_next()
        assert store.cancel(running) is False
        assert store.get(running).state is JobState.RUNNING


class TestDedupe:
    def test_track_history_round_trips(self, store: JobStore):
        store.record_track(
            video_id="vid00000001",
            path="/music/A/Singles/T.m4a",
            title="T",
            artist="A",
            tags={"title": "T", "artist": "A"},
        )
        record = store.find_track("vid00000001")
        assert record.path == "/music/A/Singles/T.m4a"
        assert record.tags == {"title": "T", "artist": "A"}

    def test_recording_the_same_video_twice_updates_rather_than_duplicates(self, store: JobStore):
        store.record_track(video_id="vid00000001", path="/old.m4a")
        store.record_track(video_id="vid00000001", path="/new.m4a")
        assert store.find_track("vid00000001").path == "/new.m4a"

    def test_forgetting_a_track_allows_a_re_ingest(self, store: JobStore):
        store.record_track(video_id="vid00000001", path="/gone.m4a")
        store.forget_track("vid00000001")
        assert store.find_track("vid00000001") is None

    def test_forgetting_by_path_is_what_a_library_delete_has(self, store: JobStore):
        """``DELETE /library`` knows the file it removed, not the video that produced it."""
        store.record_track(video_id="vid00000001", path="/music/A/Singles/T.m4a")
        store.record_track(video_id="vid00000002", path="/music/A/Singles/U.m4a")

        assert store.forget_path("/music/A/Singles/T.m4a") == 1

        assert store.find_track("vid00000001") is None
        assert store.find_track("vid00000002") is not None

    def test_forgetting_a_path_that_was_never_recorded_is_not_an_error(self, store: JobStore):
        """Most of the library arrived by ingest, but a file somebody copied in has no history
        entry, and deleting it must not be a failure."""
        assert store.forget_path("/music/A/Singles/copied-in-by-hand.m4a") == 0


class TestRecovery:
    def test_a_job_left_running_by_a_crash_is_requeued(self, store: JobStore):
        job_id = _enqueue(store, max_attempts=3)
        store.claim_next()

        assert store.requeue_running() == 1
        job = store.get(job_id)
        assert job.state is JobState.QUEUED
        assert job.attempts == 1  # the attempt still counts
        assert "requeued" in job.error

    def test_a_job_that_exhausted_its_attempts_is_not_requeued_forever(self, store: JobStore):
        job_id = _enqueue(store, max_attempts=1)
        store.claim_next()

        assert store.requeue_running() == 0
        job = store.get(job_id)
        assert job.state is JobState.FAILED
        assert "out of attempts" in job.error


class TestListing:
    def test_jobs_are_listed_newest_first_and_can_be_filtered(self, store: JobStore):
        old = _enqueue(store, "vid00000001", now=1000.0)
        new = _enqueue(store, "vid00000002", now=2000.0)
        store.claim_next()  # claims `old`

        assert [job.id for job in store.list_jobs()] == [new, old]
        assert [job.id for job in store.list_jobs(states=[JobState.RUNNING])] == [old]
        assert [job.id for job in store.list_jobs(states=[JobState.QUEUED])] == [new]

    def test_counts_cover_every_state(self, store: JobStore):
        _enqueue(store, "vid00000001")
        assert store.counts() == {"queued": 1, "running": 0, "done": 0, "failed": 0}


class TestRobustness:
    def test_a_failed_begin_does_not_leave_the_write_lock_held(self, store: JobStore):
        """`__exit__` never runs when `__enter__` raises, so releasing there is the only chance;
        getting it wrong deadlocks every later write in the process."""
        with pytest.raises(sqlite3.OperationalError):
            with store._writing():  # noqa: SLF001 - exercising the transaction helper directly
                with store._writing():  # noqa: SLF001 - nested BEGIN is the failure being tested
                    pass  # pragma: no cover

        done = threading.Event()

        def write() -> None:
            _enqueue(store, "vid00000002")
            done.set()

        thread = threading.Thread(target=write)
        thread.start()
        thread.join(timeout=5)
        assert done.is_set(), "the write lock was orphaned"

    def test_a_closed_store_stays_closed(self, tmp_path: Path):
        """A store that silently reopened would let a worker thread outlive the shutdown that
        closed it and write to a database nobody is reading."""
        store = JobStore(tmp_path / "jobs.db")
        _enqueue(store)
        store.close()

        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            store.list_jobs()

    def test_marking_a_finished_job_failed_does_not_rewrite_history(self, store: JobStore):
        job_id = _enqueue(store)
        store.claim_next()
        store.mark_done(job_id, path="/music/x.m4a", tags=None, already_present=False)

        store.mark_failed(job_id, "something threw after the file landed")
        job = store.get(job_id)
        assert job.state is JobState.DONE
        assert job.path == "/music/x.m4a"
