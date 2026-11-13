"""Synthetic regression reproduction; deliberately reports the RC defect."""
from pathlib import Path
import sys,json
snapshot=Path(Path('.audit_tmp/phase3-snapshot.txt').read_text().strip())
sys.path.insert(0,str(snapshot/'backend'))
from app.services.tender.ingest import RequirementRecord
from app.services.tender.pipeline import _extract_all
from app.schemas.enums import RequirementType
source='Synthetic sequence 1; synthetic dimension 6 in'
common=dict(item_id='synthetic-item',page=1,source_table_id='synthetic-table',
            source_row_id='synthetic-row',source_text=source,source_role='boq')
context=RequirementRecord(text='Synthetic sequence 1',requirement_type=RequirementType.INFORMATIONAL,
                          context_only=True,**common)
technical=RequirementRecord(text='Synthetic dimension 6 in',requirement_type=RequirementType.TECHNICAL,
                            attribute='synthetic_dimension',value='6',unit='inch',**common)
class Forbidden:
    def complete_json(self,*args,**kwargs):raise AssertionError('No model calls in a synthetic resume reproduction')
rows,calls,hits,limited,_=_extract_all([context,technical],client=Forbidden())
result={'synthetic':True,'input_sibling_facts':2,'retained_facts':len(rows),
        'technical_sibling_preserved':any(r.attribute=='synthetic_dimension' for r in rows),
        'model_calls':calls,'cause':'source-level dedup runs before per-record context handling'}
assert len(rows)==1 and rows[0].context_only and not calls and not limited
print(json.dumps(result))
Path('.audit_tmp/phase3-measurement/resume-sibling-reproduction.json').write_text(json.dumps(result,indent=2)+'\n')
