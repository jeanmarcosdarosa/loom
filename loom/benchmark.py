import os
import sys
import shutil
import parsable
from distributions.fileutil import tempdir
from distributions.io.stream import open_compressed, protobuf_stream_load
from loom.util import mkdir_p, rm_rf
import loom.config
import loom.runner
import loom.generate
import loom.datasets
import loom.schema_pb2
parsable = parsable.Parsable()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
DATASETS = os.path.join(DATA, 'datasets')
CHECKPOINTS = os.path.join(DATA, 'checkpoints/{}')
RESULTS = os.path.join(DATA, 'results')
INIT = os.path.join(DATASETS, '{}/init.pb.gz')
ROWS = os.path.join(DATASETS, '{}/rows.pbs.gz')
MODEL = os.path.join(DATASETS, '{}/model.pb.gz')
GROUPS = os.path.join(DATASETS, '{}/groups')
ASSIGN = os.path.join(DATASETS, '{}/assign.pbs.gz')


def checkpoint_files(path, suffix=''):
    path = os.path.abspath(str(path))
    assert os.path.exists(path), path
    return {
        'model' + suffix: os.path.join(path, 'model.pb.gz'),
        'groups' + suffix: os.path.join(path, 'groups'),
        'assign' + suffix: os.path.join(path, 'assign.pbs.gz'),
        'checkpoint' + suffix: os.path.join(path, 'checkpoint.pb.gz'),
    }


def list_options_and_exit(*required):
    print 'try one of:'
    for name in sorted(loom.datasets.CONFIGS):
        print '  {}'.format(name)
    sys.exit(1)


parsable.command(loom.runner.profilers)


@parsable.command
def shuffle(name=None, debug=False, profile='time'):
    '''
    Shuffle dataset for inference.
    '''
    if name is None:
        list_options_and_exit(ROWS)

    rows_in = ROWS.format(name)
    assert os.path.exists(rows_in), 'First load dataset'

    destin = os.path.join(RESULTS, name)
    mkdir_p(destin)
    rows_out = os.path.join(destin, 'rows.pbs.gz')

    loom.runner.shuffle(
        rows_in=rows_in,
        rows_out=rows_out,
        debug=debug,
        profile=profile)
    assert os.path.exists(rows_out)


@parsable.command
def infer(
        name=None,
        extra_passes=loom.config.DEFAULTS['schedule']['extra_passes'],
        debug=False,
        profile='time'):
    '''
    Run inference on a dataset, or list available datasets.
    '''
    if name is None:
        list_options_and_exit(ROWS)

    init = INIT.format(name)
    rows = ROWS.format(name)
    assert os.path.exists(init), 'First load dataset'
    assert os.path.exists(rows), 'First load dataset'
    assert extra_passes > 0, 'cannot initialize with extra_passes = 0'

    destin = os.path.join(RESULTS, name)
    mkdir_p(destin)
    groups_out = os.path.join(destin, 'groups')
    mkdir_p(groups_out)

    config = {'schedule': {'extra_passes': extra_passes}}
    config_in = os.path.join(destin, 'config.pb.gz')
    loom.config.config_dump(config, config_in)

    loom.runner.infer(
        config_in=config_in,
        rows_in=rows,
        model_in=init,
        groups_out=groups_out,
        debug=debug,
        profile=profile)

    assert os.listdir(groups_out), 'no groups were written'
    group_counts = []
    for f in os.listdir(groups_out):
        group_count = 0
        for _ in protobuf_stream_load(os.path.join(groups_out, f)):
            group_count += 1
        group_counts.append(group_count)
    print 'group_counts: {}'.format(' '.join(map(str, group_counts)))


@parsable.command
def load_checkpoint(name=None, period_sec=5, debug=False):
    '''
    Grab last full checkpoint for profiling, or list available datasets.
    '''
    if name is None:
        list_options_and_exit(MODEL)

    rows = ROWS.format(name)
    model = MODEL.format(name)
    assert os.path.exists(model), 'First load dataset'
    assert os.path.exists(rows), 'First load dataset'

    destin = CHECKPOINTS.format(name)
    rm_rf(destin)
    mkdir_p(os.path.dirname(destin))

    def load_checkpoint(name):
        checkpoint = loom.schema_pb2.Checkpoint()
        with open_compressed(checkpoint_files(name)['checkpoint']) as f:
            checkpoint.ParseFromString(f.read())
        return checkpoint

    with tempdir(cleanup_on_error=(not debug)):

        config = {'schedule': {'checkpoint_period_sec': period_sec}}
        config_in = os.path.abspath('config.pb.gz')
        loom.config.config_dump(config, config_in)

        # run first iteration
        step = 0
        mkdir_p(str(step))
        kwargs = checkpoint_files(str(step), '_out')
        print 'running checkpoint {}, tardis_iter 0'.format(step)
        loom.runner.infer(
            config_in=config_in,
            rows_in=rows,
            model_in=model,
            debug=debug,
            **kwargs)
        checkpoint = load_checkpoint(step)

        # find penultimate checkpoint
        while not checkpoint.finished:
            rm_rf(str(step - 3))
            step += 1
            print 'running checkpoint {}, tarids_iter {}'.format(
                step,
                checkpoint.tardis_iter)
            kwargs = checkpoint_files(step - 1, '_in')
            mkdir_p(str(step))
            kwargs.update(checkpoint_files(step, '_out'))
            loom.runner.infer(
                config_in=config_in,
                rows_in=rows,
                debug=debug,
                **kwargs)
            checkpoint = load_checkpoint(step)

        print 'final checkpoint {}, tardis_iter {}'.format(
            step,
            checkpoint.tardis_iter)

        last_full = str(step - 2)
        assert os.path.exists(last_full), 'too few checkpoints'
        checkpoint = load_checkpoint(step)
        print 'saving checkpoint {}, tardis_iter {}'.format(
            last_full,
            checkpoint.tardis_iter)
        shutil.move(last_full, destin)


@parsable.command
def infer_checkpoint(name=None, period_sec=0, debug=False, profile='time'):
    '''
    Run inference from checkpoint, or list available checkpoints.
    '''
    if name is None:
        list_options_and_exit(CHECKPOINTS)

    rows = ROWS.format(name)
    model = MODEL.format(name)
    checkpoint = CHECKPOINTS.format(name)
    assert os.path.exists(rows), 'First load dataset'
    assert os.path.exists(model), 'First load dataset'
    assert os.path.exists(checkpoint), 'First load checkpoint'

    destin = os.path.join(RESULTS, name)
    mkdir_p(destin)

    config = {'schedule': {'checkpoint_period_sec': period_sec}}
    config_in = os.path.join(destin, 'config.pb.gz')
    loom.config.config_dump(config, config_in)

    kwargs = {'debug': debug, 'profile': profile}
    kwargs.update(checkpoint_files(checkpoint, '_in'))

    loom.runner.infer(config_in=config_in, rows_in=rows, **kwargs)


@parsable.command
def generate(
        feature_type='mixed',
        rows=10000,
        cols=100,
        density=0.5,
        debug=False,
        profile='time'):
    '''
    Generate a synthetic dataset.
    '''
    root = os.path.abspath(os.path.curdir)
    with tempdir(cleanup_on_error=(not debug)):
        init_out = os.path.abspath('init.pb.gz')
        rows_out = os.path.abspath('rows.pbs.gz')
        model_out = os.path.abspath('model.pb.gz')
        groups_out = os.path.abspath('groups')

        os.chdir(root)
        loom.generate.generate(
            row_count=rows,
            feature_count=cols,
            feature_type=feature_type,
            density=density,
            init_out=init_out,
            rows_out=rows_out,
            model_out=model_out,
            groups_out=groups_out,
            debug=debug,
            profile=profile)

        print 'model file is {} bytes'.format(os.path.getsize(model_out))
        print 'rows file is {} bytes'.format(os.path.getsize(rows_out))


@parsable.command
def clean():
    '''
    Clean out results.
    '''
    if os.path.exists(RESULTS):
        shutil.rmtree(RESULTS)


if __name__ == '__main__':
    parsable.dispatch()