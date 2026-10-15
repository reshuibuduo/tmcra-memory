"""Synthetic provenance regressions; no model, network or user database."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'src' / 'tmcra_local'
def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
actor = load('actor_provenance')
# Package import permits evidence_view to reuse provenance helpers without loading models.
import sys, types
package = types.ModuleType('origin_fixture'); package.__path__ = [str(ROOT)]
sys.modules['origin_fixture'] = package
spec = importlib.util.spec_from_file_location('origin_fixture.evidence_view', ROOT/'evidence_view.py')
evidence = importlib.util.module_from_spec(spec); spec.loader.exec_module(evidence)

class OriginProvenanceTests(unittest.TestCase):
    def test_role_alone_is_not_human(self):
        meta = actor.normalize_message_actor_metadata('user', {'actor_type':'human'})
        self.assertFalse(actor.permits_human_preference(meta))
        self.assertEqual(actor.source_authority(meta), 'unknown_origin_source')
        # Legacy immutable actor bytes and identity hash remain stable.
        self.assertEqual(actor.actor_metadata_json(meta), '{"actor_role":"user"}')

    def test_explicit_origin_is_bounded_and_preserved(self):
        meta = actor.normalize_message_actor_metadata('user', {'origin_kind':'app_generated',
            'capture_method':'host_event_metadata', 'provenance_strength':'host_declared'})
        self.assertEqual(actor.graph_actor_metadata(meta)['origin_kind'], 'app_generated')
        self.assertFalse(actor.permits_human_preference(meta))
        self.assertEqual(actor.source_authority(meta, derived=True), 'derived_app_generated_memory')
        for bad in ({'origin_kind':'verified_human'}, {'origin_kind':'human_authored','provenance_strength':'authenticated'}):
            with self.assertRaises(actor.ActorProvenanceError):
                actor.normalize_message_actor_metadata('user', bad)
        with self.assertRaises(actor.ActorProvenanceError):
            actor.normalize_message_actor_metadata('assistant', {'origin_kind':'human_authored'})

    def test_human_declaration_not_authentication(self):
        meta = actor.normalize_message_actor_metadata('user', {'origin_kind':'human_authored'})
        self.assertEqual(meta['provenance_strength'], 'caller_declared')
        self.assertEqual(meta['capture_method'], 'explicit_api_declaration')
        self.assertTrue(actor.permits_human_preference(meta))

    def test_render_never_promotes_role_or_stale_authority(self):
        for origin in ('unknown','app_generated','automation'):
            row = {'actor_role':'user','authority':'user_source','text':'Synthetic quoted [app suggestion] stays verbatim.',
                   'origin_kind':origin, 'memory_contexts':[{'actor_role':'user','authority':'derived_user_memory',
                   'origin_kind':origin,'claim_text':'Synthetic derived note'}]}
            out=evidence.build_prompt_evidence({'evidence_windows':[row]},selected_route='raw')['content']
            self.assertNotIn('authority=user_source',out)
            self.assertNotIn('authority=derived_user_memory',out)
            self.assertNotIn('User requirements and facts',out)
            self.assertIn(row['text'],out)
            self.assertIn('origin='+origin,out)

    def test_source_lineage_wins_over_derived_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'g.sqlite'
            with sqlite3.connect(db) as c:
                c.execute('CREATE TABLE records(scope_id TEXT,memory_id TEXT,metadata_json TEXT)')
                c.execute('INSERT INTO records VALUES(?,?,?)',('s','source',json.dumps({
                    'content_variant':'source_message','actor_role':'user','origin_kind':'automation',
                    'capture_method':'host_event_metadata','provenance_strength':'host_declared'})))
            data={'source_record_id':'source','actor_role':'user','origin_kind':'human_authored',
                  'authority':'derived_user_memory','claim_text':'synthetic'}
            out=actor.enrich_evidence_actor_provenance(data,database=db,scope_id='s')
            self.assertEqual(out['origin_kind'],'automation')
            self.assertNotEqual(out['authority'],'derived_user_memory')
            self.assertEqual(data['origin_kind'],'human_authored')

    def test_missing_or_mixed_source_does_not_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'g.sqlite'
            with sqlite3.connect(db) as c:
                c.execute('CREATE TABLE records(scope_id TEXT,memory_id TEXT,metadata_json TEXT)')
                c.execute('INSERT INTO records VALUES(?,?,?)',('s','human',json.dumps({
                    'content_variant':'source_message','actor_role':'user','origin_kind':'human_authored',
                    'capture_method':'explicit_api_declaration','provenance_strength':'caller_declared'})))
            out=actor.enrich_evidence_actor_provenance({'source_record_ids':['human','missing'],
                'actor_role':'user','origin_kind':'human_authored','authority':'user_source'},database=db,scope_id='s')
            self.assertEqual(out['origin_kind'],'unknown')
            self.assertEqual(out['authority'],'unknown_origin_source')

    def test_compiled_stale_authority_is_not_preserved(self):
        out=evidence.build_prompt_evidence({'compiled_evidence_packet':{'claims':[{'actor_role':'user',
            'authority':'user_assertion','claim_text':'synthetic'}]}},selected_route='compiled')
        row=json.loads(out['content'])['claims'][0]
        self.assertEqual(row['origin_kind'],'unknown')
        self.assertEqual(row['authority'],'derived_unknown_origin_memory')

if __name__ == '__main__': unittest.main()
