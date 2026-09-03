import random
from cockpit.utils.sorting import natural_sort_key

def test_natural_sort_key_fixed_point():
    expected = ['1', '2', '10', '37A', '37B', 'C1', 'C2', 'C10']
    shuffled = expected.copy()
    random.seed(42)
    random.shuffle(shuffled)
    assert sorted(shuffled, key=natural_sort_key) == expected

def test_natural_sort_key_superscript_regression():
    assert natural_sort_key('²') == ['²']

def test_natural_sort_key_none_handling():
    assert natural_sort_key(None) == []
    assert natural_sort_key(None) < natural_sort_key('1')

def test_natural_sort_key_differing_shapes():
    key1 = natural_sort_key('C1')
    key2 = natural_sort_key('37A')
    _ = key1 < key2
