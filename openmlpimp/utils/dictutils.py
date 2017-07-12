import collections
import scipy

def rank_dict(dictionary, reverse=False):
    if reverse:
        for key in dictionary.keys():
            dictionary[key] = 1 - dictionary[key]
    sortdict = collections.OrderedDict(sorted(dictionary.items()))
    ranks = scipy.stats.rankdata(list(sortdict.values()))
    result = {}
    for idx, (key, value) in enumerate(sortdict.items()):
        result[key] = ranks[idx]
    return result


def sum_dict_values(a, b):
    result = {}
    if set(a.keys()) != set(b.keys()):
        raise ValueError('keys not the same')
    for idx in a.keys():
        result[idx] = a[idx] + b[idx]
    return result


def divide_dict_values(d, denominator):
    result = {}
    for idx in d.keys():
        result[idx] = d[idx] + denominator
    return result