import os
import sys
import shutil
import tempfile
import contextlib
from itertools import imap, product
from nose.tools import assert_true, assert_false, assert_equal
import numpy
import numpy.random
from distributions.tests.util import seed_all
from distributions.util import scores_to_probs
from distributions.io.stream import protobuf_stream_load, protobuf_stream_dump
from distributions.lp.models import dd, dpd, nich, gp
from distributions.lp.clustering import PitmanYor
from distributions.util import multinomial_goodness_of_fit
import loom.schema_pb2
import loom.runner
import loom.util
import parsable
parsable = parsable.Parsable()

assert dd and dpd and gp and nich  # pacify pyflakes

CWD = os.getcwd()

TRUNCATE_COUNT = 32
MIN_GOODNESS_OF_FIT = 1e-3
SCORE_TOL = 1e-1  # FIXME why does this need to be so large?
SEED = 123456789

CLUSTERING = PitmanYor.from_dict({'alpha': 2.0, 'd': 0.1})

FEATURE_TYPES = {
    'dd': dd,
    'dpd': dpd,
    'gp': gp,
    'nich': nich,
}

DENSITIES = [
    1.0,
    0.5,
    0.0,
]

# Cross Cat Latent Space Sizes up to 10000000, generated by:
# python src/test/test_posterior_enum.py datasets 10000000
LATENT_SIZES = [
    [1, 1, 2, 5, 15, 52, 203, 877, 4140, 21147, 115975, 678570, 4213597],
    [1, 1, 2, 5, 15, 52, 203, 877, 4140, 21147, 115975, 678570, 4213597],
    [1, 2, 6, 22, 94, 454, 2430, 14214, 89918, 610182, 4412798],
    [1, 5, 30, 205, 1555, 12880, 115155, 1101705],
    [1, 15, 240, 4065, 72465, 1353390],
    [1, 52, 2756, 148772, 8174244],
    [1, 203, 41412, 8489257],
    [1, 877, 770006],
    [1, 4140],
    [1, 21147],
    [1, 115975],
    [1, 678570],
    [1, 4213597],
]

CAT_MAX_SIZE = 100000
KIND_MAX_SIZE = 205


@contextlib.contextmanager
def chdir(wd):
    oldwd = os.getcwd()
    try:
        os.chdir(wd)
        yield wd
    finally:
        os.chdir(oldwd)


@contextlib.contextmanager
def tempdir(cleanup_on_error=True):
    oldwd = os.getcwd()
    wd = tempfile.mkdtemp()
    try:
        os.chdir(wd)
        yield wd
        cleanup_on_error = True
    finally:
        os.chdir(oldwd)
        if cleanup_on_error:
            shutil.rmtree(wd)


if __name__ == '__main__' and sys.stdout.isatty():
    colorize = {
        'Warn': '\x1b[33mWarn\x1b[0m',
        'Fail': '\x1b[31mFail\x1b[0m',
        'Pass': '\x1b[32mPass\x1b[0m',
    }
else:
    colorize = {}


def LOG(prefix, casename, comment=''):
    prefix = colorize.get(prefix, prefix)
    message = '{: <4} {: <18} {}'.format(prefix, casename, comment)
    sys.stdout.write(message)
    sys.stdout.write('\n')
    sys.stdout.flush()
    return message


@parsable.command
def infer_cats(max_size=CAT_MAX_SIZE, debug=False):
    '''
    Test category inference.
    '''
    dimensions = [
        (object_count, feature_count)
        for object_count, sizes in enumerate(LATENT_SIZES)
        for feature_count, size in enumerate(sizes)
        if object_count > 1 and feature_count > 0 and size <= max_size
    ]
    datasets = product(dimensions, FEATURE_TYPES, DENSITIES, [False], [debug])
    datasets = list(datasets)
    parallel_map = map if debug else loom.util.parallel_map
    errors = sum(parallel_map(_test_dataset, datasets), [])
    message = '\n'.join(['Failed {} Cases:'.format(len(errors))] + errors)
    assert_false(errors, message)


@parsable.command
def infer_kinds(max_size=KIND_MAX_SIZE, debug=False):
    '''
    Test kind inference.
    '''
    dimensions = [
        (object_count, feature_count)
        for object_count, sizes in enumerate(LATENT_SIZES)
        for feature_count, size in enumerate(sizes)
        if object_count > 0 and feature_count > 0 and size <= max_size
        if object_count + feature_count > 2
    ]

    datasets = product(dimensions, FEATURE_TYPES, DENSITIES, [True], [debug])

    datasets = list(datasets)
    parallel_map = map if debug else loom.util.parallel_map
    errors = sum(parallel_map(_test_dataset, datasets), [])
    message = '\n'.join(['Failed {} Cases:'.format(len(errors))] + errors)
    assert_false(errors, message)


# Run tiny examples through nose and expensive examples by hand.
def test_cat_inference():
    infer_cats(100)


# Run tiny examples through nose and expensive examples by hand.
def test_kind_inference():
    infer_kinds(100)


def _test_dataset((dim, feature_type, density, infer_kinds, debug)):
    seed_all(SEED)
    object_count, feature_count = dim
    with tempdir(cleanup_on_error=(not debug)):

        config_name = os.path.abspath('config.pb')
        model_name = os.path.abspath('model.pb')
        rows_name = os.path.abspath('rows.pbs')

        model = generate_model(feature_count, feature_type)
        dump_model(model, model_name)

        rows = generate_rows(
            object_count,
            feature_count,
            feature_type,
            density)
        dump_rows(rows, rows_name)

        if infer_kinds:
            sample_count = 10 * LATENT_SIZES[object_count][feature_count]
            iterations = 32
        else:
            sample_count = 10 * LATENT_SIZES[object_count][1]
            iterations = 0
        config = {
            'posterior_enum': {
                'sample_count': sample_count,
                'sample_skip': 10,
            },
            'kernels': {'kind': {'iterations': iterations}},
        }
        loom.config.config_dump(config, config_name)

        casename = '{}-{}-{}-{}-{}'.format(
            object_count,
            feature_count,
            feature_type,
            density,
            config['kernels']['kind']['iterations'])
        #LOG('Run', casename)
        error = _test_dataset_config(
            casename,
            object_count,
            feature_count,
            config_name,
            model_name,
            rows_name,
            config,
            debug)
        return [] if error is None else [error]


def _test_dataset_config(
        casename,
        object_count,
        feature_count,
        config_name,
        model_name,
        rows_name,
        config,
        debug):
    sample_count = config['posterior_enum']['sample_count']
    counts_dict = {}
    scores_dict = {}
    samples = generate_samples(model_name, rows_name, config_name, debug)
    actual_count = 0
    for sample, score in samples:
        actual_count += 1
        if sample in counts_dict:
            counts_dict[sample] += 1
            expected = scores_dict[sample]
            assert abs(score - expected) < SCORE_TOL, \
                'inconsistent score: {} vs {}'.format(score, expected)
        else:
            counts_dict[sample] = 1
            scores_dict[sample] = score
    assert_equal(actual_count, sample_count)

    latents = scores_dict.keys()
    actual_latent_count = len(latents)
    infer_kinds = (config['kernels']['kind']['iterations'] > 0)
    if infer_kinds:
        expected_latent_count = count_crosscats(object_count, feature_count)
    else:
        expected_latent_count = BELL_NUMBERS[object_count]
    assert actual_latent_count <= expected_latent_count, 'programmer error'
    if actual_latent_count < expected_latent_count:
        LOG('Warn', casename, 'found only {} / {} latents'.format(
            actual_latent_count,
            expected_latent_count))

    counts = numpy.array([counts_dict[key] for key in latents])
    scores = numpy.array([scores_dict[key] for key in latents])
    probs = scores_to_probs(scores)

    highest_by_prob = numpy.argsort(probs)[::-1][:TRUNCATE_COUNT]
    is_accurate = lambda p: sample_count * p * (1 - p) >= 1
    highest_by_prob = [i for i in highest_by_prob if is_accurate(probs[i])]
    highest_by_count = numpy.argsort(counts)[::-1][:TRUNCATE_COUNT]
    highest = list(set(highest_by_prob) | set(highest_by_count))
    truncated = len(highest_by_prob) < len(probs)
    if len(highest_by_prob) < 1:
        LOG('Warn', casename, 'test is inaccurate; use more samples')
        return None

    goodness_of_fit = multinomial_goodness_of_fit(
        probs[highest_by_prob],
        counts[highest_by_prob],
        total_count=sample_count,
        truncated=truncated)

    comment = 'goodness of fit = {:0.3g}'.format(goodness_of_fit)
    if goodness_of_fit > MIN_GOODNESS_OF_FIT:
        LOG('Pass', casename, comment)
        return None
    else:
        print 'EXPECT\tACTUAL\tCHI\tVALUE'
        lines = [(probs[i], counts[i], latents[i]) for i in highest]
        for prob, count, latent in sorted(lines, reverse=True):
            expect = prob * sample_count
            chi = (count - expect) * expect ** -0.5
            pretty = pretty_latent(latent)
            print '{:0.1f}\t{}\t{:+0.1f}\t{}'.format(
                expect,
                count,
                chi,
                pretty)
        return LOG('Fail', casename, comment)


def generate_model(feature_count, feature_type):
    module = FEATURE_TYPES[feature_type]
    shared = module.Shared.from_dict(module.EXAMPLES[-1]['shared'])
    cross_cat = loom.schema_pb2.CrossCat()
    kind = cross_cat.kinds.add()
    CLUSTERING.dump_protobuf(kind.product_model.clustering.pitman_yor)
    features = getattr(kind.product_model, feature_type)
    for featureid in xrange(feature_count):
        shared.dump_protobuf(features.add())
        kind.featureids.append(featureid)
        cross_cat.featureid_to_kindid.append(0)
    CLUSTERING.dump_protobuf(cross_cat.feature_clustering.pitman_yor)
    return cross_cat


def test_generate_model():
    for feature_type in FEATURE_TYPES:
        generate_model(10, feature_type)


def dump_model(model, model_name):
    with open(model_name, 'wb') as f:
        f.write(model.SerializeToString())


def generate_rows(object_count, feature_count, feature_type, density):
    assert object_count > 0, object_count
    assert feature_count > 0, feature_count
    assert 0 <= density and density <= 1, density

    # generate structure
    feature_assignments = CLUSTERING.sample_assignments(feature_count)
    kind_count = len(set(feature_assignments))
    object_assignments = [
        CLUSTERING.sample_assignments(object_count)
        for _ in xrange(kind_count)
    ]
    group_counts = [
        len(set(assignments))
        for assignments in object_assignments
    ]

    # generate data
    module = FEATURE_TYPES[feature_type]
    shared = module.Shared.from_dict(module.EXAMPLES[0]['shared'])

    def sampler_create():
        group = module.Group()
        group.init(shared)
        sampler = module.Sampler()
        sampler.init(shared, group)
        return sampler

    table = [[None] * feature_count for _ in xrange(object_count)]
    for f, k in enumerate(feature_assignments):
        samplers = [sampler_create() for _ in xrange(group_counts[k])]
        for i, g in enumerate(object_assignments[k]):
            if numpy.random.uniform() < density:
                table[i][f] = samplers[g].eval(shared)
    return table


def test_generate_rows():
    table = generate_rows(100, 100, 'nich', 1.0)
    assert_true(all(cell is not None for row in table for cell in row))

    table = generate_rows(100, 100, 'nich', 0.0)
    assert_true(all(cell is None for row in table for cell in row))

    table = generate_rows(100, 100, 'nich', 0.5)
    assert_true(any(cell is None for row in table for cell in row))
    assert_true(any(cell is not None for row in table for cell in row))


def serialize_rows(table):
    message = loom.schema_pb2.SparseRow()
    for i, values in enumerate(table):
        message.id = i
        for value in values:
            message.data.observed.append(value is not None)
            if value is None:
                pass
            elif isinstance(value, bool):
                message.data.booleans.append(value)
            elif isinstance(value, int):
                message.data.counts.append(value)
            elif isinstance(value, float):
                message.data.reals.append(value)
            else:
                raise ValueError('unknown value type: {}'.format(value))
        yield message.SerializeToString()
        message.Clear()


def dump_rows(table, rows_name):
    protobuf_stream_dump(serialize_rows(table), rows_name)


def test_dump_rows():
    for feature_type in FEATURE_TYPES:
        table = generate_rows(10, 10, feature_type, 0.5)
        with tempdir():
            rows_name = os.path.abspath('rows.pbs')
            dump_rows(table, rows_name)
            message = loom.schema_pb2.SparseRow()
            for string in protobuf_stream_load(rows_name):
                message.ParseFromString(string)
                #print message


def generate_samples(model_name, rows_name, config_name, debug):
    with tempdir(cleanup_on_error=(not debug)):
        samples_name = os.path.abspath('samples.pbs.gz')
        with chdir(CWD):
            loom.runner.posterior_enum(
                config_name,
                model_name,
                rows_name,
                samples_name)
        message = loom.schema_pb2.PosteriorEnum.Sample()
        for string in protobuf_stream_load(samples_name):
            message.ParseFromString(string)
            sample = parse_sample(message)
            score = float(message.score)
            yield sample, score


def parse_sample(message):
    return frozenset(
        (
            frozenset(kind.featureids),
            frozenset(frozenset(group.rowids) for group in kind.groups)
        )
        for kind in message.kinds
    )


def pretty_kind(kind):
    featureids, groups = kind
    return '{} |{}|'.format(
        ' '.join(imap(str, sorted(featureids))),
        '|'.join(sorted(
            ' '.join(imap(str, sorted(group)))
            for group in groups
        ))
    )


def pretty_latent(latent):
    return ' - '.join(sorted(pretty_kind(kind) for kind in latent))


#-----------------------------------------------------------------------------
# dataset suggestions

def enum_partitions(count):
    if count == 0:
        yield ()
    elif count == 1:
        yield ((1,),)
    else:
        for p in enum_partitions(count - 1):
            yield p + ((count,),)
            for i, part in enumerate(p):
                yield p[:i] + (part + (count,),) + p[1 + i:]


BELL_NUMBERS = [
    1, 1, 2, 5, 15, 52, 203, 877, 4140, 21147, 115975, 678570, 4213597,
    27644437, 190899322, 1382958545, 10480142147, 82864869804, 682076806159,
]


def test_enum_partitions():
    for i, bell_number in enumerate(BELL_NUMBERS):
        if bell_number < 1e6:
            count = sum(1 for _ in enum_partitions(i))
            assert_equal(count, bell_number)


def count_crosscats(rows, cols):
    return sum(
        BELL_NUMBERS[rows] ** len(kinds)
        for kinds in enum_partitions(cols))


@parsable.command
def datasets(max_count=1000000):
    '''
    Suggest datasets based on bounded latent space size.
    '''
    enum_partitions
    max_rows = 16
    max_cols = 12
    print '# Cross Cat Latent Space Sizes up to {}, generated by:'.format(
        max_count)
    print 'LATENT_SIZES = ['
    for rows in range(1 + max_rows):
        counts = []
        for cols in range(1 + max_cols):
            count = count_crosscats(rows, cols)
            if count > max_count:
                break
            counts.append(count)
        if len(counts) > 1:
            print '    [{}],'.format(', '.join(map(str, counts)))
    print ']'


if __name__ == '__main__':
    parsable.dispatch()
