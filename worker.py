"""Local automatic scanner reconciliation and update-stream worker."""

import os
import signal
import threading
import time

import c8lab
import database
import scanner
import updates


def run_worker(stop_event=None, initial_backoff=2, maximum_backoff=30):
    """Reconcile desired parties, stream updates, and retry without data resets."""

    stop_event = stop_event or threading.Event()
    database.create_tables()
    database.update_scanner_runtime("starting", worker_pid=os.getpid())
    failures = 0

    try:
        while not stop_event.is_set():
            attempt_started = time.monotonic()
            try:
                if database.party_catalog_is_empty():
                    database.update_scanner_runtime("discovering")
                    scanner.refresh_party_catalog()

                if not database.get_desired_parties():
                    database.seed_default_selection(scanner.DEFAULT_PARTIES)

                database.update_scanner_runtime("reconciling")
                scanner.bootstrap_or_reconcile()
                if stop_event.is_set():
                    break

                updates.run_stream(stop_requested=stop_event.is_set)
                failures = 0
            except updates.SelectionChangePending:
                failures = 0
                continue
            except Exception as error:
                if time.monotonic() - attempt_started >= 30:
                    failures = 0
                failures += 1
                delay = min(
                    maximum_backoff,
                    max(
                        initial_backoff,
                        initial_backoff * (2 ** min(failures - 1, 10)),
                    ),
                )
                database.update_scanner_runtime("retrying", error=error)
                print("\nlocal scanner worker retrying:")
                print(str(error)[:1000])
                if hasattr(c8lab, "_tok"):
                    c8lab._tok.clear()
                if stop_event.wait(delay):
                    break
    finally:
        database.update_scanner_runtime("stopped")


def main():
    stop_event = threading.Event()

    def request_stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    run_worker(stop_event)


if __name__ == "__main__":
    main()
