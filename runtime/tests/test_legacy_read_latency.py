"""Runs unchanged against baseline and modified source; no new queue imports."""
import threading
from conftest import FakeMemoryModel


def test_read_not_held_behind_model_call(engine):
    values = dict(project_id='latency', session_id='latency-s', role='user', content='SYNTHETIC LATENCY',
                  source_app='test', native_thread_id='latency-s', native_message_id='a',
                  actor={'origin_kind':'human_authored','capture_method':'explicit_api_declaration','provenance_strength':'caller_declared'})
    engine.ingest_message(**values)
    entered, release, read_done = threading.Event(), threading.Event(), threading.Event()
    errors = []
    class Slow(FakeMemoryModel):
        def complete(self, payload):
            entered.set()
            if not release.wait(5):
                raise AssertionError('bounded synthetic writer timeout')
            return super().complete(payload)
    engine.writer.flash_client = Slow()
    def writer():
        try:
            engine.ingest_message(**{**values, 'native_message_id':'b'})
        except BaseException as exc:
            errors.append(exc)
    def reader():
        try:
            engine.recall(project_id='latency', query='LATENCY')
        except BaseException as exc:
            errors.append(exc)
        finally:
            read_done.set()
    write_thread = threading.Thread(target=writer)
    read_thread = threading.Thread(target=reader)
    write_thread.start()
    try:
        assert entered.wait(3)
        read_thread.start()
        assert read_done.wait(1), 'recall is blocked behind the model inference call'
    finally:
        release.set(); write_thread.join(5)
        if read_thread.ident:
            read_thread.join(5)
        if hasattr(engine, 'close'):
            engine.close()
    assert not errors
