import logging
import os
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    NamedTuple,
    Optional,
    Tuple,
    Type,
    Union,
)

from funcy import collecting, first, isa, join, reraise

from dvc.exceptions import DvcException
from dvc.parsing.interpolate import ParseError
from dvc.utils.objects import cached_property

from .context import (
    Context,
    ContextError,
    KeyNotInContext,
    MergeError,
    Node,
    VarsAlreadyLoaded,
)
from .interpolate import (
    check_recursive_parse_errors,
    is_interpolated_string,
    recurse,
    to_str,
)

if TYPE_CHECKING:
    from typing import NoReturn

    from dvc.repo import Repo
    from dvc.types import DictStrAny

    from .context import SeqOrMap


logger = logging.getLogger(__name__)

STAGES_KWD = "stages"
VARS_KWD = "vars"
WDIR_KWD = "wdir"
PARAMS_KWD = "params"
FOREACH_KWD = "foreach"
DO_KWD = "do"

DEFAULT_PARAMS_FILE = "params.yaml"

JOIN = "@"


class ResolveError(DvcException):
    pass


class EntryNotFound(DvcException):
    pass


def _format_preamble(msg: str, path: str, spacing: str = " ") -> str:
    return f"failed to parse {msg} in '{path}':{spacing}"


def format_and_raise(exc: Exception, msg: str, path: str) -> "NoReturn":
    spacing = (
        "\n" if isinstance(exc, (ParseError, MergeError, VarsAlreadyLoaded)) else " "
    )
    message = _format_preamble(msg, path, spacing) + str(exc)

    # FIXME: cannot reraise because of how we log "cause" of the exception
    # the error message is verbose, hence need control over the spacing
    _reraise_err(ResolveError, message, from_exc=exc)


def _reraise_err(
    exc_cls: Type[Exception], *args, from_exc: Optional[Exception] = None
) -> "NoReturn":
    err = exc_cls(*args)
    if from_exc and logger.isEnabledFor(logging.DEBUG):
        raise err from from_exc
    raise err


def check_syntax_errors(
    definition: "DictStrAny", name: str, path: str, where: str = "stages"
):
    for key, d in definition.items():
        try:
            check_recursive_parse_errors(d)
        except ParseError as exc:
            format_and_raise(exc, f"'{where}.{name}.{key}'", path)


def is_map_or_seq(data: Any) -> bool:
    _is_map_or_seq = isa(Mapping, Sequence)
    return not isinstance(data, str) and _is_map_or_seq(data)


def split_foreach_name(name: str) -> Tuple[str, Optional[str]]:
    group, *keys = name.rsplit(JOIN, maxsplit=1)
    return group, first(keys)


def check_interpolations(data: "DictStrAny", where: str, path: str):
    def func(s: "DictStrAny") -> None:
        if is_interpolated_string(s):
            raise ResolveError(
                _format_preamble(f"'{where}'", path) + "interpolating is not allowed"
            )

    return recurse(func)(data)


Definition = Union["ForeachDefinition", "EntryDefinition"]


def make_definition(
    resolver: "DataResolver", name: str, definition: "DictStrAny", **kwargs
) -> Definition:
    args = resolver, resolver.context, name, definition
    if FOREACH_KWD in definition:
        return ForeachDefinition(*args, **kwargs)
    return EntryDefinition(*args, **kwargs)


class DataResolver:
    def __init__(self, repo: "Repo", wdir: str, d: dict):
        self.fs = fs = repo.fs

        if os.path.isabs(wdir):
            wdir = fs.path.relpath(wdir)
            wdir = "" if wdir == os.curdir else wdir

        self.wdir = wdir
        self.relpath = fs.path.normpath(fs.path.join(self.wdir, "dvc.yaml"))

        vars_ = d.get(VARS_KWD, [])
        check_interpolations(vars_, VARS_KWD, self.relpath)
        self.context: Context = Context()

        try:
            args = fs, vars_, wdir  # load from `vars` section
            self.context.load_from_vars(*args, default=DEFAULT_PARAMS_FILE)
        except ContextError as exc:
            format_and_raise(exc, "'vars'", self.relpath)

        # we use `tracked_vars` to keep a dictionary of used variables
        # by the interpolated entries.
        self.tracked_vars: Dict[str, Mapping] = {}

        stages_data = d.get(STAGES_KWD, {})
        # we wrap the definitions into ForeachDefinition and EntryDefinition,
        # that helps us to optimize, cache and selectively load each one of
        # them as we need, and simplify all of this DSL/parsing logic.
        self.definitions: Dict[str, Definition] = {
            name: make_definition(self, name, definition)
            for name, definition in stages_data.items()
        }

    def resolve_one(self, name: str):
        group, key = split_foreach_name(name)

        if not self._has_group_and_key(group, key):
            raise EntryNotFound(f"Could not find '{name}'")

        # all of the checks for `key` not being None for `ForeachDefinition`
        # and/or `group` not existing in the `interim`, etc. should be
        # handled by the `self.has_key()` above.
        definition = self.definitions[group]
        if isinstance(definition, EntryDefinition):
            return definition.resolve()

        assert key
        return definition.resolve_one(key)

    def resolve(self):
        """Used for testing purposes, otherwise use resolve_one()."""
        data = join(map(self.resolve_one, self.get_keys()))
        logger.trace("Resolved dvc.yaml:\n%s", data)  # type: ignore[attr-defined]
        return {STAGES_KWD: data}

    def has_key(self, key: str):
        return self._has_group_and_key(*split_foreach_name(key))

    def _has_group_and_key(self, group: str, key: Optional[str] = None):
        try:
            definition = self.definitions[group]
        except KeyError:
            return False

        if key:
            return isinstance(definition, ForeachDefinition) and definition.has_member(
                key
            )
        return not isinstance(definition, ForeachDefinition)

    @collecting
    def get_keys(self):
        for name, definition in self.definitions.items():
            if isinstance(definition, ForeachDefinition):
                yield from definition.get_generated_names()
                continue
            yield name

    def track_vars(self, name: str, vars_) -> None:
        self.tracked_vars[name] = vars_


class EntryDefinition:
    def __init__(
        self,
        resolver: DataResolver,
        context: Context,
        name: str,
        definition: "DictStrAny",
        where: str = STAGES_KWD,
    ):
        self.resolver = resolver
        self.wdir = self.resolver.wdir
        self.relpath = self.resolver.relpath
        self.context = context
        self.name = name
        self.definition = definition
        self.where = where

    def resolve(self, **kwargs):
        try:
            return self.resolve_stage(**kwargs)
        except ContextError as exc:
            format_and_raise(exc, f"stage '{self.name}'", self.relpath)

    def resolve_stage(self, skip_checks: bool = False) -> "DictStrAny":
        context = self.context
        name = self.name
        if not skip_checks:
            # we can check for syntax errors as we go for interpolated entries,
            # but for foreach-generated ones, once is enough, which it does
            # that itself. See `ForeachDefinition.do_definition`.
            check_syntax_errors(self.definition, name, self.relpath)

        # we need to pop vars from generated/evaluated data
        definition = deepcopy(self.definition)
        with context.track() as tracked_data:
            resolved = {
                key: self._resolve(context, value, key, skip_checks)
                for key, value in definition.items()
            }

        self.resolver.track_vars(name, tracked_data)
        return {name: resolved}

    def _resolve(
        self, context: "Context", value: Any, key: str, skip_checks: bool
    ) -> "DictStrAny":
        try:
            return context.resolve(
                value, skip_interpolation_checks=skip_checks, key=key
            )
        except (ParseError, KeyNotInContext) as exc:
            format_and_raise(exc, f"'{self.where}.{self.name}.{key}'", self.relpath)


class IterationPair(NamedTuple):
    key: str = "key"
    value: str = "item"


class ForeachDefinition:
    def __init__(
        self,
        resolver: DataResolver,
        context: Context,
        name: str,
        definition: "DictStrAny",
        where: str = STAGES_KWD,
    ):
        self.resolver = resolver
        self.relpath = self.resolver.relpath
        self.context = context
        self.name = name

        assert DO_KWD in definition
        self.foreach_data = definition[FOREACH_KWD]
        self._do_definition = definition[DO_KWD]

        self.pair = IterationPair()
        self.where = where

    @cached_property
    def do_definition(self):
        # optimization: check for syntax errors only once for `foreach` stages
        check_syntax_errors(self._do_definition, self.name, self.relpath)
        return self._do_definition

    @cached_property
    def resolved_iterable(self):
        return self._resolve_foreach_data()

    def _resolve_foreach_data(self) -> "SeqOrMap":
        try:
            iterable = self.context.resolve(self.foreach_data, unwrap=False)
        except (ContextError, ParseError) as exc:
            format_and_raise(exc, f"'{self.where}.{self.name}.foreach'", self.relpath)

        # foreach data can be a resolved dictionary/list.
        self._check_is_map_or_seq(iterable)
        # foreach stages will have `item` and `key` added to the context
        # so, we better warn them if they have them already in the context
        # from the global vars. We could add them in `set_temporarily`, but
        # that'd make it display for each iteration.
        self._warn_if_overwriting(self._inserted_keys(iterable))
        return iterable

    def _check_is_map_or_seq(self, iterable):
        if not is_map_or_seq(iterable):
            node = iterable.value if isinstance(iterable, Node) else iterable
            typ = type(node).__name__
            raise ResolveError(
                f"failed to resolve '{self.where}.{self.name}.foreach'"
                f" in '{self.relpath}': expected list/dictionary, got " + typ
            )

    def _warn_if_overwriting(self, keys: List[str]):
        warn_for = [k for k in keys if k in self.context]
        if warn_for:
            linking_verb = "is" if len(warn_for) == 1 else "are"
            logger.warning(
                (
                    "%s %s already specified, "
                    "will be overwritten for stages generated from '%s'"
                ),
                " and ".join(warn_for),
                linking_verb,
                self.name,
            )

    def _inserted_keys(self, iterable) -> List[str]:
        keys = [self.pair.value]
        if isinstance(iterable, Mapping):
            keys.append(self.pair.key)
        return keys

    @cached_property
    def normalized_iterable(self):
        """Convert sequence to Mapping with keys normalized."""
        iterable = self.resolved_iterable
        if isinstance(iterable, Mapping):
            return {to_str(k): v for k, v in iterable.items()}

        assert isinstance(iterable, Sequence)
        if any(map(is_map_or_seq, iterable)):
            # if the list contains composite data, index are the keys
            return {to_str(idx): value for idx, value in enumerate(iterable)}

        # for simple lists, eg: ["foo", "bar"],  contents are the key itself
        return {to_str(value): value for value in iterable}

    def has_member(self, key: str) -> bool:
        return key in self.normalized_iterable

    def get_generated_names(self):
        return list(map(self._generate_name, self.normalized_iterable))

    def _generate_name(self, key: str) -> str:
        return f"{self.name}{JOIN}{key}"

    def resolve_all(self) -> "DictStrAny":
        return join(map(self.resolve_one, self.normalized_iterable))

    def resolve_one(self, key: str) -> "DictStrAny":
        return self._each_iter(key)

    def _each_iter(self, key: str) -> "DictStrAny":
        err_message = f"Could not find '{key}' in foreach group '{self.name}'"
        with reraise(KeyError, EntryNotFound(err_message)):
            value = self.normalized_iterable[key]

        # NOTE: we need to use resolved iterable/foreach-data,
        # not the normalized ones to figure out whether to make item/key
        # available
        inserted = self._inserted_keys(self.resolved_iterable)
        temp_dict = {self.pair.value: value}
        key_str = self.pair.key
        if key_str in inserted:
            temp_dict[key_str] = key

        with self.context.set_temporarily(temp_dict, reserve=True):
            # optimization: item and key can be removed on __exit__() as they
            # are top-level values, and are not merged recursively.
            # This helps us avoid cloning context, which is slower
            # (increasing the size of the context might increase
            # the no. of items to be generated which means more cloning,
            # i.e. quadratic complexity).
            generated = self._generate_name(key)
            entry = EntryDefinition(
                self.resolver, self.context, generated, self.do_definition
            )
            try:
                # optimization: skip checking for syntax errors on each foreach
                # generated stages. We do it once when accessing do_definition.
                return entry.resolve_stage(skip_checks=True)
            except ContextError as exc:
                format_and_raise(exc, f"stage '{generated}'", self.relpath)

            # let mypy know that this state is unreachable as format_and_raise
            # does not return at all (it's not able to understand it for some
            # reason)
            raise AssertionError("unreachable")
