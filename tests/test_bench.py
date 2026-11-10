import numpy as np
from typer.testing import CliRunner

from timelens.bench.generate import random_model, random_rules
from timelens.cli import app
from timelens.model.process import ProcessGraph


def test_generator_is_seeded_and_valid():
    a = ProcessGraph(random_model(30, np.random.default_rng(3)))
    b = ProcessGraph(random_model(30, np.random.default_rng(3)))
    assert len(a.tasks) == 30 and a.topo == b.topo
    rules = random_rules(a, np.random.default_rng(3))
    rules.check_against(set(a.tasks), set(a.nodes), set(a.flows))


def test_cli_bench_writes_files(tmp_path):
    res = CliRunner().invoke(app, ["bench", "--sizes", "10,25", "--models", "2",
                                   "--out-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "bench.csv").exists() and (tmp_path / "bench.png").exists()


def test_sizes_must_be_positive_integers():
    import pytest

    from timelens.bench.run import parse_sizes
    from timelens.errors import TimeLensError

    assert parse_sizes("10, 25") == (10, 25)
    for bad in ("0", "10,x", ""):
        with pytest.raises(TimeLensError, match="positive integers"):
            parse_sizes(bad)
