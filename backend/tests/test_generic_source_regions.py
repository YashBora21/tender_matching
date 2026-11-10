"""Synthetic non-GeM layouts and unfamiliar labels; no vendor source fixtures."""
import pymupdf
import pytest
from app.services.tender.ingest import read_specification


def write_table(path, label, value, *, neighbouring="", empty=False):
    doc=pymupdf.open();page=doc.new_page()
    for y in (40,120):page.draw_line((35,y),(320,y))
    for x in (35,165,320):page.draw_line((x,40),(x,120))
    page.insert_text((45,65),label,fontsize=9)
    if not empty:page.insert_text((175,65),value,fontsize=9)
    if neighbouring:page.insert_text((355,65),neighbouring,fontsize=9)
    page.insert_text((45,170),'Synthetic document context retained for review. No inferred values.',fontsize=9)
    doc.save(path);doc.close()


@pytest.mark.parametrize('label,value',[
    ('Supplier prerequisites','Authority document.\nSupporting annexure required.'),
    ('Environmental envelope','-20 to 80 C.\nMaximum pressure 12 bar.'),
    ('Delivery arrangements','Within 45 days.\nPayment after inspection.'),
    ('Unfamiliar field Z9','Document reference ABC.\nCondition stated elsewhere.')])
def test_unknown_labels_and_wrapped_values_preserve_source_cells(tmp_path,label,value):
    path=tmp_path/'synthetic-layout.pdf';write_table(path,label,value)
    read=read_specification(path,wrapper=True)
    rows=[r for r in read.rows if r.get('source_cells')]
    assert len(rows)==1
    assert rows[0]['source_cells'][0]==label
    assert rows[0]['source_cells'][1]==value
    assert all(line in rows[0]['description'] for line in value.splitlines())
    assert rows[0]['source_bbox'][2]==320


def test_narrow_table_does_not_remove_parallel_technical_text(tmp_path):
    path=tmp_path/'synthetic-columns.pdf'
    write_table(path,'Supplier prerequisites','Authority document',neighbouring='Required pressure >= 12 bar')
    read=read_specification(path,wrapper=True)
    fields=[r for r in read.rows if r.get('source_cells')]
    prose=[r for r in read.rows if not r.get('source_cells')]
    assert len(fields)==1
    assert 'Required pressure' not in fields[0]['description']
    assert any('Required pressure >= 12 bar' in r['description'] for r in prose)


def test_empty_cell_does_not_inherit_or_invent_a_value(tmp_path):
    path=tmp_path/'synthetic-empty.pdf';write_table(path,'Unresolved supplier condition','',empty=True)
    read=read_specification(path,wrapper=True)
    assert not any(r.get('source_cells') for r in read.rows)
    assert any('Unresolved supplier condition' in r['description'] for r in read.rows)


def test_identical_value_in_two_fields_keeps_distinct_source_rows(tmp_path):
    path=tmp_path/'synthetic-duplicates.pdf'
    doc=pymupdf.open();page=doc.new_page()
    for y in (40,80,120):page.draw_line((35,y),(500,y))
    for x in (35,220,500):page.draw_line((x,40),(x,120))
    for y,label in ((65,'Supplier prerequisites'),(105,'Product qualification')):
        page.insert_text((45,y),label)
        page.insert_text((230,y),'Certificate in annexure')
    doc.save(path);doc.close()
    rows=[r for r in read_specification(path,wrapper=True).rows if r.get('source_cells')]
    assert len(rows)==2 and rows[0]['source_row_id']!=rows[1]['source_row_id']
    assert rows[0]['title']!=rows[1]['title']
    assert rows[0]['source_cells'][1]==rows[1]['source_cells'][1]
