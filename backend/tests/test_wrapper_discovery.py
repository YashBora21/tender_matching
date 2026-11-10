"""Synthetic wrapper sections, specifications and buyer terms."""
import pymupdf
from app.services.tender.ingest import _discovery_bounds, read_specification, assign_to_items, TenderItem
from app.services.tender.classify import DISCOVERY
from app.services.requirement.retype import retype
from app.schemas.enums import RequirementType


def test_closed_section_keeps_continuation_but_not_following_atc():
    texts = ["Bid details\nSearched String: synthetic pump\nCatalogue head 20 m",
             "continued catalogue size 10 mm\nCategories selected for notification\nBuyer added terms: pump shall have head 50 m"]
    bounds = _discovery_bounds(texts)
    assert set(bounds) == {1, 2}
    assert "continued catalogue" in texts[1][slice(*bounds[2])]
    assert "Buyer added" not in texts[1][slice(*bounds[2])]
    assert "Bid details" not in texts[0][slice(*bounds[1])]


def test_unclosed_section_is_not_excluded():
    assert _discovery_bounds(["Searched String: synthetic pump\npump shall have head 50 m"]) == {}
    assert _discovery_bounds(["Categories selected for notification"]) == {}


def test_wrapper_retains_search_text_as_information_and_keeps_buyer_terms(tmp_path):
    path = tmp_path / "synthetic-wrapper.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 50), "Searched String: synthetic pump\nCatalogue head 20 m\nCategories selected for notification\nBuyer added terms: pump shall have head 50 m")
    doc.save(path)
    doc.close()
    read = read_specification(path, wrapper=True)
    discovery = [r for r in read.rows if r['page_tag'] == DISCOVERY]
    assert len(discovery) == 1 and "Catalogue head 20 m" in discovery[0]['description']
    assert any("head 50 m" in r['description'] and r['page_tag'] != DISCOVERY for r in read.rows)
    assigned, _ = assign_to_items(discovery, [TenderItem(item_id="1", title="synthetic pump")])
    assert assigned[0].requirement_type == RequirementType.INFORMATIONAL
    assert not retype(assigned[0])
    assert all(r['page_tag'] != DISCOVERY for r in read_specification(path).rows)


def test_exact_boq_heading_is_metadata_but_technical_clause_is_retained(tmp_path):
    from app.services.tender.classify import WRAPPER_METADATA
    path = tmp_path / "synthetic-heading.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 50), "SYN-123 Gasket Cylinder Head")
    page.insert_text((50, 90), "The gasket shall withstand pressure 12 bar.")
    doc.save(path)
    doc.close()
    read = read_specification(path, wrapper=True, item_titles=("SYN-123 Gasket Cylinder Head",))
    assert any(r['page_tag'] == WRAPPER_METADATA and 'source_bbox' in r for r in read.rows)
    assert any('12 bar' in r['description'] and r['page_tag'] != WRAPPER_METADATA for r in read.rows)
    assert all('SYN-123 Gasket Cylinder Head' not in r['description'] for r in read.rows if r['page_tag'] != WRAPPER_METADATA)
    assert read.pages[0].status == 'parsed'


def test_labelled_item_category_table_is_metadata_not_a_specification(tmp_path):
    from app.services.tender.classify import WRAPPER_METADATA
    path = tmp_path / "synthetic-labelled-field.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    for y in (40, 80):
        page.draw_line((40, y), (550, y))
    for x in (40, 220, 550):
        page.draw_line((x, 40), (x, 80))
    page.insert_text((50, 65), "Item Category")
    page.insert_text((230, 65), "SYN-123 Cylinder Head")
    page.insert_text((50, 120), "Buyer requirement: pressure must be 12 bar.")
    doc.save(path)
    doc.close()
    read = read_specification(path, wrapper=True)
    assert any(r['page_tag'] == WRAPPER_METADATA and 'Cylinder Head' in r['description'] for r in read.rows)
    assert any('12 bar' in r['description'] and r['page_tag'] != WRAPPER_METADATA for r in read.rows)
    assert all(r['page_tag'] != WRAPPER_METADATA for r in read_specification(path).rows)


def test_wrapper_field_keeps_certificate_and_requesting_label_in_one_row(tmp_path):
    from app.services.tender.ingest import assign_to_items
    from app.services.requirement.atomize import atomize_records
    path = tmp_path / 'synthetic-field-context.pdf'
    doc = pymupdf.open()
    page = doc.new_page()
    for y in (40, 110):
        page.draw_line((40,y),(550,y))
    for x in (40,220,550):
        page.draw_line((x,40),(x,110))
    page.insert_text((50,65),'Documents required')
    page.insert_text((50,85),'from seller')
    page.insert_text((230,65),'Authorization document.')
    page.insert_text((230,85),'Certificate requested in annexure.')
    page.insert_text((50,160),'The supplied pump shall provide head 50 m.')
    doc.save(path);doc.close()
    read = read_specification(path,wrapper=True)
    fields = [r for r in read.rows if r.get('source_cells')]
    assert len(fields)==1
    field=fields[0]
    assert 'from seller' in field['title']
    assert 'Certificate requested' in field['description']
    assert field['source_row_id'] and field['source_bbox']
    from app.services.tender.ingest import RequirementRecord
    record = RequirementRecord(text=field['description'], item_id='', source_row_id=field['source_row_id'],
                               source_section=field['title'], extraction_method=field['extraction_method'])
    assert atomize_records([record]) == [record]
    assert any('head 50 m' in r['description'] for r in read.rows if not r.get('source_cells'))
