"""XML entity-declaration guard tests (offline, inline fixtures -- no files, no network).

These cover the one XML attack a Tableau migration realistically faces: a ``.twb`` or ``.tds``
that expands into far more data than it appears to contain. The asserts pin (a) that a real
Tableau prolog is untouched, (b) that an entity-declaring DOCTYPE is refused with a message
naming the file, and (c) that the check cannot be tricked by, or false-positive on, content
inside the document body.
"""
import zipfile

import pytest

from xml_safety import UnsafeXmlDocument, reject_entity_declarations
from workbook_table_calcs import load_workbook_xml


BILLION_LAUGHS = """<?xml version='1.0' encoding='utf-8' ?>
<!DOCTYPE workbook [
 <!ENTITY a "AAAAAAAAAA">
 <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
]>
<workbook source-build='2021.4'><datasources/></workbook>"""

BENIGN = """<?xml version='1.0' encoding='utf-8' ?>

<!-- a comment Tableau sometimes writes -->
<workbook source-build='2021.4'><datasources/></workbook>"""


def test_a_normal_tableau_workbook_passes_through_untouched():
    assert reject_entity_declarations(BENIGN) is BENIGN


def test_a_byte_order_mark_does_not_hide_the_declaration():
    with pytest.raises(UnsafeXmlDocument):
        reject_entity_declarations("\ufeff" + BILLION_LAUGHS)


def test_an_entity_declaring_doctype_is_refused():
    with pytest.raises(UnsafeXmlDocument) as excinfo:
        reject_entity_declarations(BILLION_LAUGHS)
    assert "declares XML entities" in str(excinfo.value)


def test_the_refusal_names_the_file_so_an_estate_run_is_actionable():
    with pytest.raises(UnsafeXmlDocument) as excinfo:
        reject_entity_declarations(BILLION_LAUGHS, source="Regional Sales.twbx")
    assert "Regional Sales.twbx" in str(excinfo.value)


def test_a_doctype_without_entity_declarations_is_left_alone():
    # Harmless and not the attack; refusing it would only reject valid work.
    doc = ("<?xml version='1.0' ?>\n<!DOCTYPE workbook SYSTEM 'workbook.dtd'>\n"
           "<workbook><datasources/></workbook>")
    assert reject_entity_declarations(doc) is doc


def test_a_calculation_mentioning_entity_syntax_is_not_mistaken_for_an_attack():
    # Only the prolog can legally carry a DOCTYPE, so a literal `<!ENTITY` in the body -- here
    # inside CDATA, where it is legal -- never triggers the guard.
    doc = ("<?xml version='1.0' ?>\n<workbook><calculation>"
           "<![CDATA[IF [x] = '<!ENTITY a \"b\">' THEN 1 END]]>"
           "</calculation></workbook>")
    assert reject_entity_declarations(doc) is doc


def test_an_empty_or_missing_document_is_not_an_error_here():
    # Emptiness is a different failure with a better message downstream; not this guard's job.
    assert reject_entity_declarations("") == ""
    assert reject_entity_declarations(None) is None


def test_loading_a_packaged_workbook_applies_the_guard(tmp_path):
    twbx = tmp_path / "hostile.twbx"
    with zipfile.ZipFile(twbx, "w") as z:
        z.writestr("hostile.twb", BILLION_LAUGHS)
    with pytest.raises(UnsafeXmlDocument):
        load_workbook_xml(str(twbx))


def test_loading_a_bare_workbook_applies_the_guard(tmp_path):
    twb = tmp_path / "hostile.twb"
    twb.write_text(BILLION_LAUGHS, encoding="utf-8")
    with pytest.raises(UnsafeXmlDocument):
        load_workbook_xml(str(twb))


def test_a_clean_workbook_still_loads(tmp_path):
    twb = tmp_path / "fine.twb"
    twb.write_text(BENIGN, encoding="utf-8")
    assert "<workbook" in load_workbook_xml(str(twb))
