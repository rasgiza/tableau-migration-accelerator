"""Refuse Tableau documents that declare their own XML entities.

Tableau writes plain XML: a short declaration, then the root element. It never emits a
``<!DOCTYPE>`` and never declares entities. A workbook or datasource that does is therefore
either corrupt or hostile, and it costs almost nothing to say so before parsing it.

Why this specific check, and not a general size limit:

* The stdlib parser this engine uses (``xml.etree.ElementTree``) already refuses to resolve
  **external** entities, so a crafted workbook cannot make a run read a local file or reach
  out to the network. That -- the dangerous one -- is closed before we start.
* What the parser does not cap is **internal** entity expansion. A few hundred bytes of
  nested declarations expand to gigabytes during parsing and take the process out; the
  classic "billion laughs". The entity *declaration* is what makes that possible, so
  refusing the declaration closes it exactly, without imposing a document size limit that a
  legitimately huge estate workbook would trip over.

The check reads only the prolog -- the text before the root element begins -- because that is
the only place a DOCTYPE may legally appear. A calculation whose formula happens to contain
the characters ``<!ENTITY`` is therefore never mistaken for an attack.
"""

import re

__all__ = ["UnsafeXmlDocument", "reject_entity_declarations"]

# The root element is the first tag that starts with a name character. Everything before it is
# the prolog: the XML declaration, comments, processing instructions and (illegally, for
# Tableau) a DOCTYPE. `<?xml ...?>`, `<!-- ... -->` and `<!DOCTYPE ...>` all start `<?` or `<!`,
# so they can never be mistaken for the root element.
_ROOT_ELEMENT_RE = re.compile(r"<[A-Za-z_]")
_DOCTYPE_RE = re.compile(r"<!DOCTYPE", re.IGNORECASE)
_ENTITY_DECL_RE = re.compile(r"<!ENTITY", re.IGNORECASE)


class UnsafeXmlDocument(ValueError):
    """A document declares XML entities, which no Tableau export ever does."""


def reject_entity_declarations(xml_text, *, source=""):
    """Return ``xml_text`` unchanged, or raise :class:`UnsafeXmlDocument`.

    ``source`` is a human label (a file path or workbook name) used in the message so an
    estate run names the offending file rather than just failing.
    """
    text = (xml_text or "").lstrip("\ufeff")
    root = _ROOT_ELEMENT_RE.search(text)
    prolog = text[:root.start()] if root else text
    if not _DOCTYPE_RE.search(prolog) or not _ENTITY_DECL_RE.search(prolog):
        return xml_text
    where = f" in '{source}'" if source else ""
    raise UnsafeXmlDocument(
        f"refusing to parse XML{where}: it declares XML entities in a DOCTYPE. Tableau never "
        "writes one, so this file is either corrupt or crafted to expand into far more data "
        "than it appears to contain. Re-export the asset from Tableau; if it is genuinely "
        "yours, strip the DOCTYPE and re-run.")
