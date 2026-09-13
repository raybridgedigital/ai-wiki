import pytest
from backend.quotes import source_quote


def test_nasa_caption_typography_is_restored():
    original='The rover\'s home in the “clay unit,” as scientists describe it. Credits: NASA/JPL'
    generated='The rover\'s home in the "clay unit," as scientists describe it. Credits: NASA/JPL.'
    assert source_quote(generated, 'Heading\n'+original+'\nNext section')==original


@pytest.mark.parametrize('quote,text',[
    ('Cost is $50.', 'Cost is $500.'),
    ('Mars has life.', 'Mars may have life.'),
    ('Age is 4.5.', 'Age is 45.'),
    ('red-blue', 'red blue'),
    ('NASA.', 'NASA explores Mars.'),
    ('Life on Mars', 'Life\non Mars\nLife\t on Mars'),
    ('"Mars"', '“Mars” and ”Mars“'),
])
def test_no_fuzzy_or_ambiguous_repair(quote,text):
    assert source_quote(quote,text)==quote


def test_exact_matches_preserved_even_when_repeated():
    assert source_quote('Mars.', 'Mars. Mars.')=='Mars.'
