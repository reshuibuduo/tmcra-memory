import copy
import pytest
from tmcra_local.personal_knowledge import validate_personal_knowledge_batch, PersonalKnowledgeError, PERSONAL_KNOWLEDGE_DOMAIN_SCHEMA_VERSION
from tmcra_local.visual_atlas import _identity_fields

HUMAN={'actor_role':'user','origin_kind':'human_authored','capture_method':'explicit_api_declaration','provenance_strength':'caller_declared'}
def display(**kw): return {'zh':kw,'en':kw}
def fixture(origin,collection='project',status='confirmed'):
    batch={'domain_id':'d','batch_id':'b','evidence':[{'id':'e',**origin}]}
    result={'schema_version':PERSONAL_KNOWLEDGE_DOMAIN_SCHEMA_VERSION,'domain_id':'d','batch_id':'b','title':'Synthetic', 'description':'Synthetic','display':display(title='Synthetic',description='Synthetic'),
      'pages':[{'collection':collection,'page_type':'overview','title':'Synthetic','abstract':'Synthetic','display':display(title='Synthetic',abstract='Synthetic'),
        'claims':[{'text':'Synthetic fact','status':status,'evidence_ids':['e'],'display':display(text='Synthetic fact')}], 'sections':[]}],'excluded_evidence_ids':[]}
    return batch,result

@pytest.mark.parametrize('origin',[{'actor_role':'user'}, {'actor_role':'user','origin_kind':'automation'}, {'actor_role':'assistant','origin_kind':'assistant_generated'}, {**HUMAN,'origin_kind':'app_generated'}])
def test_nonhuman_cannot_be_confirmed_or_personal(origin):
    for collection,status in [('project','confirmed'),('personal','provisional')]:
        b,r=fixture(origin,collection,status)
        with pytest.raises(PersonalKnowledgeError,match='human-origin'):
            validate_personal_knowledge_batch(b,r)
    b,r=fixture(origin,'project','provisional')
    assert validate_personal_knowledge_batch(b,r)['pages'][0]['claims'][0]['status']=='provisional'


def test_explicit_declaration_supports_personal_not_authenticated():
    b,r=fixture(HUMAN,'personal')
    assert validate_personal_knowledge_batch(b,r)['pages'][0]['collection']=='personal'
    b['evidence'].append({'id':'unknown','actor_role':'user'})
    r['pages'][0]['claims'][0]['evidence_ids'].append('unknown')
    with pytest.raises(PersonalKnowledgeError): validate_personal_knowledge_batch(b,r)


def test_sections_and_atlas_identity_cannot_bypass_origin():
    b,r=fixture({'actor_role':'user'},'personal','provisional')
    page=r['pages'][0];page['claims']=[];page['sections']=[{'heading':'Fixture','body':'Fixture','evidence_ids':['e'],'display':display(heading='Fixture',body='Fixture')}]
    with pytest.raises(PersonalKnowledgeError): validate_personal_knowledge_batch(b,r)
    unknown=_identity_fields({'level':'evidence','actor_role':'user'})
    human=_identity_fields({'level':'evidence',**HUMAN})
    assert unknown['origin_kind']=='unknown'
    assert unknown!=human


def test_actual_atlas_resolves_derivation_against_original_source_origin():
    from tmcra_local.visual_atlas import build_visual_atlas
    source={'id':'raw','layer':'source','label':'Synthetic original','actor_role':'user',
            'origin_kind':'automation','capture_method':'host_event_metadata','provenance_strength':'host_declared'}
    stale={'id':'derived','layer':'fast','label':'Synthetic claim','source_record_ids':['raw'],**HUMAN}
    atlas=build_visual_atlas('synthetic',[{'session_id':'s'}],source_graphs={'s':{'nodes':[source,stale],'edges':[]}})
    derived=next(n for n in atlas['nodes'] if n.get('memory_id')=='derived')
    raw=next(n for n in atlas['nodes'] if n.get('source_record_id')=='raw')
    assert derived['origin_kind']==raw['origin_kind']=='automation'
    assert atlas['identity_manifest'][derived['id']]['origin_kind']=='automation'
    stale['source_record_ids'].append('missing')
    mixed=build_visual_atlas('synthetic',[{'session_id':'s'}],source_graphs={'s':{'nodes':[source,stale],'edges':[]}})
    assert next(n for n in mixed['nodes'] if n.get('memory_id')=='derived')['origin_kind']=='unknown'
