"""Synthetic table anchors and preservation of extraction provenance."""
from app.services.match.verify import check_source_anchor
from app.services.match.facts import ExtractedFact
import pytest


def test_bound_cell_retains_range_and_conditions():
    fact = ExtractedFact(found=True, attribute='size', value='10-16', unit='inch',
                         operator='between', conditions='Class 150 only', model='Synthetic A',
                         table_index=0,row_index=1,column_index=0)
    context={'tables':[[['Size','Conditions'],['10-16','Class 150 only']]],
             'passage':'Class 150 only', 'claim':fact.model_dump()}
    assert check_source_anchor(context,'size','10-16') is None
    assert fact.model_dump()['conditions']=='Class 150 only'


def test_wrong_column_anchor_fails():
    context={'tables':[[['Body Design','End Connections'],['Integral flange','ASME B16.5']]],
             'claim':{'table_index':0,'row_index':1,'column_index':0}}
    assert not check_source_anchor(context,'end connection','integral flange').passed


def test_missing_anchor_on_table_fails():
    context={'tables':[[['Size'],['10-16']]],'claim':{}}
    assert not check_source_anchor(context,'size','10-16').passed


def test_model_scope_is_not_borrowed_from_another_series():
    context={'document_model':'Synthetic Series A',
             'claim':{'model_or_series':'Synthetic Series B'}}
    assert not check_source_anchor(context,'size','10').passed


def test_conditions_must_be_copied_from_source():
    context={'passage':'Synthetic standard conditions',
             'claim':{'conditions':'Synthetic optional upgrade'}}
    assert not check_source_anchor(context,'size','10').passed


@pytest.mark.parametrize('column', [1, 4])
def test_adjacent_label_value_pair_binds_on_either_side_of_table(column):
    row = ['Synthetic label', 'Synthetic value', '', 'Other label', 'Other value']
    row[column - 1], row[column] = 'EndConnection', 'Flanged'
    context = {'tables': [[['Synthetic heading'] * 5, row]],
               'claim': {'table_index': 0, 'row_index': 1, 'column_index': column}}
    assert check_source_anchor(context, 'end connection', 'flanged') is None


def test_label_cannot_be_borrowed_across_empty_or_other_cells():
    context = {'tables': [[['Synthetic heading'] * 3, ['EndConnection', None, 'Flanged']]],
               'claim': {'table_index': 0, 'row_index': 1, 'column_index': 2}}
    assert not check_source_anchor(context, 'end connection', 'flanged').passed


def test_row_label_does_not_override_a_different_attribute_header():
    context = {'tables': [[['Synthetic heading', 'Body material'], ['EndConnection', 'Flanged']]],
               'claim': {'table_index': 0, 'row_index': 1, 'column_index': 1}}
    assert not check_source_anchor(context, 'end connection', 'flanged').passed


def test_label_cell_anchor_resolves_only_to_exact_adjacent_value():
    context = {'tables': [[['Synthetic heading', 'Synthetic heading'], ['EndConnection', 'Flanged']]],
               'claim': {'table_index': 0, 'row_index': 1, 'column_index': 0}}
    assert check_source_anchor(context, 'end connection', 'flanged') is None
    assert context['claim']['column_index'] == 0
    assert context['resolved_anchor']['column_index'] == 1


def test_label_cell_anchor_cannot_substitute_a_different_value():
    context = {'tables': [[['Synthetic heading', 'Synthetic heading'], ['EndConnection', 'Flanged']]],
               'claim': {'table_index': 0, 'row_index': 1, 'column_index': 0}}
    assert not check_source_anchor(context, 'end connection', 'threaded').passed


@pytest.mark.parametrize('index', [-1, True, '1'])
def test_invalid_anchor_indices_are_rejected(index):
    context = {'tables': [[['Synthetic heading', 'Synthetic heading'], ['EndConnection', 'Flanged']]],
               'claim': {'table_index': 0, 'row_index': index, 'column_index': 1}}
    assert not check_source_anchor(context, 'end connection', 'flanged').passed


@pytest.mark.parametrize('label_row', [False, True])
def test_open_attribute_labels_bind_without_vocabulary_entries(label_row):
    rows = ([['Synthetic heading'] * 5, ['Synthetic coupling geometry', 'X7', '', 'Other label', 'X7']]
            if label_row else [['Synthetic coupling geometry', 'Other'], ['X7', 'Y8']])
    column = 1 if label_row else 0
    context = {'tables': [rows], 'claim': {'table_index': 0, 'row_index': 1, 'column_index': column}}
    assert check_source_anchor(context, 'Synthetic coupling geometry', 'X7') is None
    context['claim']['column_index'] = 4 if label_row else 1
    assert not check_source_anchor(context, 'Synthetic coupling geometry', 'X7').passed


def test_numeric_punctuation_is_not_erased_from_values():
    context = {'tables': [[['Pressure'], ['13.7']]],
               'claim': {'table_index': 0, 'row_index': 1, 'column_index': 0}}
    assert not check_source_anchor(context, 'pressure', '137').passed


def test_table_model_column_cannot_borrow_another_models_value():
    context = {'document_model': 'Synthetic A',
               'tables': [[['Model', 'Size'], ['Synthetic B', '10']]],
               'claim': {'table_index': 0, 'row_index': 1, 'column_index': 1}}
    assert not check_source_anchor(context, 'size', '10').passed
    context['tables'][0][1][0] = 'Synthetic A'
    assert check_source_anchor(context, 'size', '10') is None
