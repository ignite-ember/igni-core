"""XML and HTML parsed with no entity expansion, no DTD, no network and no huge trees."""

from __future__ import annotations

from lxml import etree, html


class SafeXml:
    """The parsers every document reader that reads markup goes through; a new one per parse, as lxml's are not thread-safe."""

    @staticmethod
    def _xml_parser() -> etree.XMLParser:
        return etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            dtd_validation=False,
            huge_tree=False,
            remove_comments=True,
            remove_pis=True,
            recover=True,
        )

    @staticmethod
    def _html_parser() -> html.HTMLParser:
        return html.HTMLParser(
            encoding="utf-8",
            no_network=True,
            huge_tree=False,
            remove_comments=True,
            remove_pis=True,
            default_doctype=False,
        )

    @classmethod
    def xml(cls, data: bytes) -> etree._Element:
        """The root of ``data`` as XML; raises ``ValueError`` when nothing parses."""
        root = etree.fromstring(data, cls._xml_parser())
        if root is None:
            raise ValueError("not XML")
        return root

    @classmethod
    def markup(cls, data: bytes) -> etree._Element:
        """The root of ``data`` as XHTML, or as HTML when it is not well-formed XML."""
        try:
            root = etree.fromstring(data, cls._xml_parser())
        except etree.XMLSyntaxError:
            root = None
        if root is not None and cls.name(root) == "html":
            return root
        return cls.html(data.decode("utf-8", errors="replace"))

    @classmethod
    def html(cls, text: str) -> etree._Element:
        """The root of ``text`` as HTML; raises ``ValueError`` when it is blank."""
        if not text.strip():
            raise ValueError("empty HTML")
        return html.document_fromstring(text.encode("utf-8"), parser=cls._html_parser())

    @staticmethod
    def name(element: etree._Element) -> str:
        """``element``'s tag without its namespace, lower case; empty for a comment or PI."""
        if not isinstance(element.tag, str):
            return ""
        return etree.QName(element).localname.lower()
