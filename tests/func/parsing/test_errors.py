"""Negative tests for the parametrization."""


import logging
import re

import pytest

from dvc.parsing import ResolveError
from dvc.parsing.context import Context
from dvc.parsing.interpolate import embrace

from . import make_entry_definition, make_foreach_def


def escape_ansi(line):
    ansi_escape = re.compile(r"(\x9B|\x1B\[)[0-?]*[ -\/]*[@-~]")
    return ansi_escape.sub("", line)


# Tests for the interpolated entries


def test_failed_to_interpolate(tmp_dir, dvc):
    context = Context(models={"foo": "bar"})
    definition = make_entry_definition(
        tmp_dir, "build", {"cmd": "echo ${models.foo.}"}, context
    )

    with pytest.raises(ResolveError) as exc_info:
        definition.resolve()

    assert (
        escape_ansi(str(exc_info.value))
        == "failed to parse 'stages.build.cmd' in 'dvc.yaml':\n"
        "${models.foo.}\n"
        "            ^\n"
        "ParseException: Expected end of text, found '.'"
        "  (at char 12), (line:1, col:13)"
    )
    assert definition.context == {"models": {"foo": "bar"}}


def test_specified_key_does_not_exist(tmp_dir, dvc):
    definition = make_entry_definition(
        tmp_dir,
        "build",
        {"cmd": "echo ${models.foobar}"},
        Context(models={"foo": "foo"}),
    )
    with pytest.raises(ResolveError) as exc_info:
        definition.resolve()

    assert (
        str(exc_info.value) == "failed to parse 'stages.build.cmd' in 'dvc.yaml': "
        "Could not find 'models.foobar'"
    )
    assert definition.context == {"models": {"foo": "foo"}}


@pytest.mark.parametrize(
    "wdir, expected_msg",
    [
        ("${models[foobar]}", " Could not find 'models.foobar'"),
        (
            "${models.foo]}",
            (
                "\n${models.foo]}\n"
                "            ^\n"
                "ParseException: Expected end of text, found ']'"
                "  (at char 12), (line:1, col:13)"
            ),
        ),
    ],
)
def test_wdir_failed_to_interpolate(tmp_dir, dvc, wdir, expected_msg):
    definition = make_entry_definition(
        tmp_dir,
        "build",
        {"wdir": wdir, "cmd": "echo ${models.bar}"},
        Context(models={"bar": "bar"}),
    )
    with pytest.raises(ResolveError) as exc_info:
        definition.resolve()

    assert escape_ansi(str(exc_info.value)) == (
        "failed to parse 'stages.build.wdir' in 'dvc.yaml':" + expected_msg
    )
    assert definition.context == {"models": {"bar": "bar"}}


def test_interpolate_non_string(tmp_dir, dvc):
    definition = make_entry_definition(
        tmp_dir, "build", {"outs": "${models}"}, Context(models={})
    )
    with pytest.raises(ResolveError) as exc_info:
        definition.resolve()

    assert (
        str(exc_info.value) == "failed to parse 'stages.build.outs' in 'dvc.yaml':\n"
        "Cannot interpolate data of type 'dict'"
    )
    assert definition.context == {"models": {}}


def test_interpolate_nested_iterable(tmp_dir, dvc):
    definition = make_entry_definition(
        tmp_dir,
        "build",
        {"cmd": "echo ${models}"},
        Context(models={"list": [1, [2, 3]]}),
    )
    with pytest.raises(ResolveError) as exc_info:
        definition.resolve()

    assert (
        str(exc_info.value) == "failed to parse 'stages.build.cmd' in 'dvc.yaml':\n"
        "Cannot interpolate nested iterable in 'list'"
    )


# Tests foreach generated stages and their error messages


def test_foreach_data_syntax_error(tmp_dir, dvc):
    definition = make_foreach_def(tmp_dir, "build", "${syntax.[error}", {})
    with pytest.raises(ResolveError) as exc_info:
        definition.resolve_all()

    assert (
        escape_ansi(str(exc_info.value))
        == "failed to parse 'stages.build.foreach' in 'dvc.yaml':\n"
        "${syntax.[error}\n"
        "        ^\n"
        "ParseException: Expected end of text, found '.'"
        "  (at char 8), (line:1, col:9)"
    )


@pytest.mark.parametrize("key", ["modelss", "modelss.123"])
def test_foreach_data_key_does_not_exists(tmp_dir, dvc, key):
    definition = make_foreach_def(tmp_dir, "build", embrace(key), {})
    with pytest.raises(ResolveError) as exc_info:
        definition.resolve_all()
    assert (
        str(exc_info.value) == "failed to parse 'stages.build.foreach' in 'dvc.yaml': "
        f"Could not find '{key}'"
    )


@pytest.mark.parametrize(
    "foreach_data", ["${foo}", "${dct.model1}", "${lst.0}", "foobar"]
)
def test_foreach_data_expects_list_or_dict(tmp_dir, dvc, foreach_data):
    context = Context({"foo": "bar", "dct": {"model1": "a-out"}, "lst": ["foo", "bar"]})
    definition = make_foreach_def(tmp_dir, "build", foreach_data, {}, context)
    with pytest.raises(ResolveError) as exc_info:
        definition.resolve_all()
    assert (
        str(exc_info.value)
        == "failed to resolve 'stages.build.foreach' in 'dvc.yaml': "
        "expected list/dictionary, got str"
    )


@pytest.mark.parametrize(
    "global_data, where",
    [
        ({"item": 10, "key": 10}, "item and key are"),
        ({"item": 10}, "item is"),
        ({"key": 5}, "key is"),
    ],
)
def test_foreach_overwriting_item_in_list(tmp_dir, dvc, caplog, global_data, where):
    context = Context(global_data)
    definition = make_foreach_def(
        tmp_dir, "build", {"model1": 10, "model2": 5}, {}, context
    )
    with caplog.at_level(logging.WARNING, logger="dvc.parsing"):
        definition.resolve_all()

    assert caplog.messages == [
        f"{where} already specified, "
        "will be overwritten for stages generated from 'build'"
    ]


def test_foreach_do_syntax_errors(tmp_dir, dvc):
    definition = make_foreach_def(
        tmp_dir, "build", ["foo", "bar"], {"cmd": "echo ${syntax.[error}"}
    )

    with pytest.raises(ResolveError) as exc_info:
        definition.resolve_all()

    assert (
        escape_ansi(str(exc_info.value))
        == "failed to parse 'stages.build.cmd' in 'dvc.yaml':\n"
        "${syntax.[error}\n"
        "        ^\n"
        "ParseException: Expected end of text, found '.'"
        "  (at char 8), (line:1, col:9)"
    )


@pytest.mark.parametrize(
    "key, loc",
    [
        (
            "item.thresh",  # the `thresh` in not available on model2`
            "stages.build@1.cmd",
        ),
        ("foo.bar", "stages.build@0.cmd"),  # not available on any stages
    ],
)
def test_foreach_do_definition_item_does_not_exist(tmp_dir, dvc, key, loc):
    context = Context(foo="bar")
    definition = make_foreach_def(
        tmp_dir,
        "build",
        [{"thresh": "10"}, {}],
        {"cmd": embrace(key)},
        context,
    )

    with pytest.raises(ResolveError) as exc_info:
        definition.resolve_all()

    assert (
        str(exc_info.value)
        == f"failed to parse '{loc}' in 'dvc.yaml': Could not find '{key}'"
    )

    # should have no `item` and `key` even though it failed to resolve.
    assert context == {"foo": "bar"}


def test_foreach_wdir_key_does_not_exist(tmp_dir, dvc):
    definition = make_foreach_def(
        tmp_dir,
        "build",
        "${models}",
        {"wdir": "${ite}", "cmd": "echo ${item}"},
        Context(models=["foo", "bar"]),
    )
    with pytest.raises(ResolveError) as exc_info:
        definition.resolve_all()
    assert (
        str(exc_info.value)
        == "failed to parse 'stages.build@foo.wdir' in 'dvc.yaml': Could not find 'ite'"
    )
    assert definition.context == {"models": ["foo", "bar"]}
