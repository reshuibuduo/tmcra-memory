from tmcra_local.engine import LocalMemoryEngine

def test_opt_in_async_admission_is_explicit():
    assert callable(getattr(LocalMemoryEngine, 'enqueue_message', None)), 'Missing durable opt-in enqueue_message; only synchronous writer exists'
