Combining Documents into a Book
================================

Several independent Markdown documents can be combined into a single manual
with :class:`~mistune.MarkdownBook`.  Every document is identified by a stable
document identifier, and local references (``[home]`` reference links and
``[^1]`` footnotes) always resolve within the document that defines them.

.. code-block:: python

    from mistune import combine_markdown

    intro = "Read [home] first.[^1]\n\n[home]: /home\n[^1]: intro note\n"
    guide = "The guide has its own [^1].\n\n[^1]: guide note\n"

    result = combine_markdown([
        ("intro", intro),
        ("guide", guide),
    ])
    print(result.html)
    for diagnostic in result.diagnostics:
        print(diagnostic.format())

Each input is a ``(id, text)`` tuple or a :class:`~mistune.Document`.

Footnotes
---------

* Footnotes are numbered globally, in the order of their **first citation**
  across the whole book.
* Citing the same footnote more than once (including from other documents via
  the cross syntax below) produces a single numbered item with one back link
  per citation.
* Footnote definitions that are never cited are not rendered.
* Reordering the input documents changes the displayed numbers, but the
  generated HTML ids keep tracking document identity, for example
  ``id="fn-intro-1"`` and ``id="fnref-guide-intro-1-1"``.

Explicit Cross-Document References
----------------------------------

References never match another document by name coincidence.  Crossing a
document boundary requires an explicit syntax:

================================  ===================================
Syntax                            Meaning
================================  ===================================
``[^@doc-id:key]``                footnote ``key`` defined in ``doc-id``
``[@doc-id:label]``               shortcut reference link
``[text][@doc-id:label]``         full reference link
================================  ===================================

.. code-block:: markdown

    See the [@intro:home] link and the shared note.[^@intro:1]

Diagnostics
-----------

:meth:`MarkdownBook.compile` never raises for content problems; instead it
returns :class:`~mistune.BookResult` with a ``diagnostics`` list.  Pass
``strict=True`` to raise :class:`~mistune.BookError` instead.  Duplicate or
invalid document identifiers are always a structural error and raise
immediately.

Diagnostic codes include:

* ``unknown-link`` / ``unknown-footnote`` -- unresolved local reference
* ``unknown-cross-link`` / ``unknown-cross-footnote`` -- cross reference to a
  label/footnote that is not defined in the target document
* ``unknown-document`` -- cross reference points at an identifier that is not
  part of the book
* ``conflicting-definition`` -- a footnote or reference link is defined more
  than once with different contents in the same document
* ``duplicate-document`` -- invalid or repeated document identifier

Single document parsing with :meth:`mistune.Markdown.parse` is unaffected and
keeps its original semantics.
