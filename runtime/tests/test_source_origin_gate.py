from tmcra_local.core import v4_batch_writer as writer

HUMAN={'actor_role':'user','origin_kind':'human_authored','capture_method':'explicit_api_declaration','provenance_strength':'caller_declared'}

class NoCalls:
    def complete(self, payload):
        raise AssertionError('unknown/automatic source must not invoke extraction')


def test_unknown_and_automatic_keep_sources_without_model_calls(engine):
    engine.writer.flash_client=NoCalls()
    for index, origin in enumerate(({}, {'origin_kind':'app_generated'}, {'origin_kind':'automation'})):
        result=engine.ingest_message(project_id='synthetic-origin',session_id='s',role='user',
            content=f'Synthetic source {index}: [suggested-next-user-message] is quoted text.',
            source_app='test',native_thread_id='s',native_message_id=f'm{index}',actor=origin)
        assert result['scopes'][0]['status']=='committed'
    records=list(engine.graph_factory.for_scope('project:synthetic-origin').adapter.graph.records_by_id.values())
    assert len(records)==3
    assert all(record.metadata['content_variant']=='source_message' for record in records)
    assert not any(record.metadata.get('authority')=='user_source' for record in records)
    assert all('quoted text.' in record.metadata['raw_content'] for record in records)


def test_mixed_batch_request_does_not_feed_unknown_source_to_model():
    def message(i, actor):
        return writer.SourceMessage('s','session',0,i,str(i),'user','2026-10-16',f'fixture {i}',actor_metadata=actor)
    batch=writer.SourceBatch('s','session',0,0,(message(0,{}),message(1,HUMAN),message(2,{'origin_kind':'automation'})))
    request=writer.build_batch_request(batch)
    assert [row['message_id'] for row in request['messages']]==['1']
    result=writer.validate_batch_response({'schema_version':writer.BATCH_SCHEMA_VERSION,'batch_id':batch.batch_id,
        'messages':[{'message_id':'1','message_role':'user','assertions':[],'interactions':[],'resolutions':[]}]},batch)
    assert len(result['messages'])==1
