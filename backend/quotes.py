"""Restore typographic model quotations to exact, unambiguous source spans."""
import re


def source_quote(quote, text):
    if quote in text:
        return quote
    variants = {'"': '["“”]', '“': '["“”]', '”': '["“”]',
                "'": "['‘’]", '‘': "['‘’]", '’': "['‘’]"}

    def pattern(value):
        return r'\s+'.join(''.join(variants.get(c, re.escape(c)) for c in token)
                          for token in value.split())

    value = quote.strip()
    if not value:
        return quote
    patterns = [pattern(value)]
    # Models often add sentence punctuation to an unpunctuated caption.
    # Only restore it at an actual source line ending, never mid-sentence.
    if value.endswith('.') and not value.endswith('..') and len(value) > 1:
        patterns.append(pattern(value[:-1]) + r'(?=\r?\n|$)')
    for expression in patterns:
        matches = re.finditer(expression, text)
        first = next(matches, None)
        if first is not None:
            return first.group() if next(matches, None) is None else quote
    return quote
