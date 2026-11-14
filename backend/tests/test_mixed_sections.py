"""Synthetic mixed attachments retain all pages and never bill restricted sections."""
from dataclasses import asdict
from app.services.tender import pipeline,source_policy
from app.services.tender.ingest import SpecificationRead,PageRead,RequirementRecord
from app.schemas.enums import RequirementType


def test_section_scope_persists_across_pages_resets_at_explicit_heading_and_document(monkeypatch,tmp_path):
    spec=dict(path='synthetic-spec.pdf',kind='spec',sha256='synthetic-one')
    other=dict(path='synthetic-other.pdf',kind='spec',sha256='synthetic-two')
    def read(source,*args):
        rows=([dict(page=1,title='TECHNICAL SPECIFICATIONS',description='Synthetic pressure 8 bar'),
               dict(page=1,title='SPECIAL CONDITIONS OF CONTRACT',description='Synthetic supplier must have ISO 9001'),
               dict(page=2,title='',description='Synthetic pressure 16 bar'),
               dict(page=3,title='TECHNICAL REQUIREMENTS',description='Synthetic pressure 20 bar')]
              if source is spec else [dict(page=1,title='',description='Synthetic pressure 25 bar')])
        return SpecificationRead(rows=rows,pages=[PageRead(i,'mixed','parsed') for i in sorted({r['page'] for r in rows})])
    monkeypatch.setattr(pipeline,'_read_specification',read)
    result=pipeline._read_package('synthetic',tmp_path,[spec,other],spec,None)
    assert [r['source_role'] for r in result.rows]==['specification','atc','atc','specification','specification']
    assert [r['context_only'] for r in result.rows]==[False,True,True,False,False]
    assert len(result.pages)==4 and all(p.status=='parsed' for p in result.pages)
    restricted=[RequirementRecord(text=r['description'],item_id='',source_role=r['source_role'],
        context_only=r['context_only'],requirement_type=RequirementType(r['clause_type']))
        for r in result.rows if r['context_only']]
    class Forbidden:
        def complete_json(self,*args,**kwargs):raise AssertionError('Restricted sections must make zero calls')
    output,calls,_,limited,_=pipeline._extract_all(restricted,client=Forbidden())
    assert not calls and not limited and len(output)==2


def test_mentions_do_not_change_scope_and_selectors_keep_unchanged_classifier_keys(monkeypatch):
    assert source_policy.section_role('See special conditions of contract for delivery','specification')=='specification'
    full=source_policy.fingerprint();classifier=source_policy.fingerprint(for_classification=True)
    data=dict(source_policy.policy(),section_role_patterns={})
    monkeypatch.setattr(source_policy,'policy',lambda:data)
    assert source_policy.fingerprint()!=full
    assert source_policy.fingerprint(for_classification=True)==classifier
