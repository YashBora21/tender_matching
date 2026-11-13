"""Synthetic ruled grids; no provider calls or application database writes."""
import pytest
from app.services.tender.ingest import _read_tables, assign_to_items, TenderItem


class Page:
    def __init__(self,rows):self.rows=rows
    def extract_tables(self):return [self.rows]


def test_headerless_property_groups_keep_labels_values_and_item_numbers():
    grid=[['1','Synthetic assembly A',None],[None,'Synthetic size','10–16 inches'],
          ['',None,None],['2','Synthetic assembly B',None],[None,'Synthetic pressure','8 bar']]
    rows,state=_read_tables(Page(grid),'',1,'technical',None)
    assigned,unresolved=assign_to_items(rows,[TenderItem(item_id='t:1',title='Synthetic assembly A'),
        TenderItem(item_id='t:2',title='Synthetic assembly B')])
    assert not unresolved and [r.item_id for r in assigned]==['t:1','t:1','t:2','t:2']
    field=rows[1]
    assert field['source_headers']==['Synthetic size'] and field['source_cells']==['10–16 inches']
    assert field['source_grid_cells']==[None, 'Synthetic size','10–16 inches']
    assert field['row_text']=='Synthetic size 10–16 inches'
    assert assigned[1].table_context['layout']=='item_label_value'
    assert assigned[1].table_context['original_cells']==field['source_grid_cells']
    assert len({r.source_row_id for r in assigned})==4


def test_group_scope_continues_only_on_immediately_following_property_page():
    _,state=_read_tables(Page([['1','Synthetic assembly',None],[None,'Synthetic alpha','8 bar']]),'',1,'technical',None)
    rows,next_state=_read_tables(Page([[None,'Synthetic beta','16 bar']]),'',2,'technical',state)
    assert rows[0]['number']=='1' and rows[0]['title']=='Synthetic assembly'
    rows,_=_read_tables(Page([[None,'Synthetic beta','16 bar']]),'',3,'technical',state)
    assert rows is None
    rows,_=_read_tables(Page([['A','Synthetic prose','arbitrary']]),'',2,'mixed',state)
    assert rows is None


@pytest.mark.parametrize('source',[
    dict(number='1',title='Synthetic B'), dict(number='99',title='Synthetic A'),
    dict(number='1',title='Synthetic A',description='Synthetic B')])
def test_conflicting_or_unknown_explicit_identity_is_never_overridden(source):
    row=dict(description='Synthetic property 8 bar',**{k:v for k,v in source.items() if k!='description'})
    if 'description' in source:row['description']=source['description']
    assigned,unresolved=assign_to_items([row],[TenderItem(item_id='1',title='Synthetic A'),
                                           TenderItem(item_id='2',title='Synthetic B')])
    assert not assigned and unresolved[0]['review_required']
    assert 'conflicting' in unresolved[0]['review_reason']


def test_duplicate_numbers_and_single_item_fallback_cannot_override_explicit_identity():
    row=dict(number='1',description='Synthetic property 8 bar')
    assert not assign_to_items([row],[TenderItem(item_id='a:1',title='Synthetic A'),
                                    TenderItem(item_id='b:1',title='Synthetic B')])[0]
    assert not assign_to_items([dict(row,number='99')],[TenderItem(item_id='1',title='Synthetic A')])[0]


def test_unnumbered_empty_value_stops_inheritance_without_losing_source_rows():
    grid=[['1','Synthetic A',None],[None,'Synthetic alpha','8 bar'],
          [None,'Synthetic B',None],[None,'Synthetic beta','16 bar']]
    rows,_=_read_tables(Page(grid),'',1,'technical',None)
    assert [r['number'] for r in rows]==['1','1','','']
    assert rows[-1]['source_cells']==['16 bar']
