import logging
import sys
from collections import deque
from dataclasses import MISSING, asdict
from typing import Callable, Tuple, Type

from opsi.manager.link import Link, NodeLink, StaticLink
from opsi.manager.manager import Manager
from opsi.manager.manager_schema import Function, ModuleItem
from opsi.manager.pipeline import Connection, Links, Pipeline
from opsi.manager.types import AnyType, RangeType, Slide
from opsi.util.concurrency import FifoLock

from .schema import *

__all__ = (
    "export_manager",
    "export_nodetree",
    "import_nodetree",
    "NodeTreeImportError",
)


LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------


def _rangetype_serialize(type):
    if not isinstance(type, RangeType):
        return None

    return InputOutputF(type="Range", params=type.serialize())


def _slide_serialize(type):
    if not isinstance(type, Slide):
        return None

    return InputOutputF(type="Slide", params=type.serialize())


def _tuple_serialize(type):
    if not isinstance(type, tuple):
        return None

    return InputOutputF(type="Enum", params={"items": type})


_type_name: Dict[Type, str] = {AnyType: "Any"}
# Each item in _abnormal_types takes in a type and returns InputOutputF if the
# parser supports the type, or None if it does not
_abnormal_types: List[Callable[[Type[any]], Optional[InputOutputF]]] = [
    _slide_serialize,
    _rangetype_serialize,
    _tuple_serialize,
]  # add new type parsers here


def get_type(_type: Type) -> InputOutputF:
    if _type in (None, type(None)):
        return None

    if _type in _type_name:
        name = _type_name.get(_type)
        return InputOutputF(type=name)

    for parser in _abnormal_types:
        IO = parser(_type)

        if IO is not None:
            return IO

    return InputOutputF(type=_type.__name__)


def get_field_type(field) -> InputOutputF:
    io = get_type(field.type)
    if field.default and field.default is not MISSING:
        io.params["default"] = field.default
    return io


def get_types(types):
    # there are no instances of an input or output having type None
    return {name: get_type(_type) for name, _type in types.items()}


def get_settings_types(types):
    pruned_types = []
    # if none, just don't show it
    for _type in types:
        if type(_type.type) is not type(None):
            pruned_types.append(_type)
    return {field.name: get_field_type(field) for field in pruned_types}


def _serialize_funcs(funcs: Dict[str, Type[Function]]) -> List[FunctionF]:
    return [
        FunctionF(
            name=func.__name__,
            type=func.type,
            settings=get_settings_types(func.SettingTypes),
            inputs=get_types(func.InputTypes),
            outputs=get_types(func.OutputTypes),
        )
        for func in funcs.values()
    ]


def _serialize_modules(modules: Dict[str, ModuleItem]) -> List[ModuleF]:
    return [
        ModuleF(
            package=mod_package,
            version=mod.info.version,
            funcs=_serialize_funcs(mod.funcs),
        )
        for mod_package, mod in modules.items()
    ]


def export_manager(manager: Manager) -> SchemaF:
    return SchemaF(modules=_serialize_modules(manager.modules))


# ---------------------------------------------------------


class NodeTreeImportError(ValueError):
    def __init__(
        self, program, node: NodeN = None, msg="", *, exc_info=True, real_node=False
    ):
        program.pipeline.clear()
        program.pipeline.broken = True

        self.node = node

        # https://github.com/python/cpython/blob/10ecbadb799ddf3393d1fc80119a3db14724d381/Lib/logging/__init__.py#L1572
        if exc_info:
            if isinstance(exc_info, BaseException):
                exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
            elif not isinstance(exc_info, tuple):
                exc_info = sys.exc_info()

            msg += f": {exc_info[1]!r}"

        if real_node:
            self.type = str(self.node.func_type)
        else:
            self.type = self.node.type

        logMsg = msg
        if self.node:
            msg = f"{self.type}: {msg}"
            logMsg = f"Node '{self.node.id}' returned error {msg}"

        super().__init__(msg)

        # if exc_info == True, this class must not be instantiated outside an `except:` clause
        LOGGER.debug(f"Error during importing nodetree. {logMsg}", exc_info=exc_info)


def _process_node_links(program, node: NodeN, ids) -> List[str]:
    links: Links = {}
    empty_links: List[str] = []

    real_node = program.pipeline.nodes[node.id]

    for name in real_node.func_type.InputTypes.keys():
        input = node.inputs.get(name)

        if input is not None and input.link in ids:
            links[name] = Connection(input.link.id, input.link.name)
        else:
            # link was None, or link points to deleted node
            empty_links.append(name)

    try:
        program.pipeline.create_links(node.id, links)
    except KeyError:  # idk why this happens
        raise NodeTreeImportError(program, msg="Unknown Error, please try again")

    # empty_links used by _process_node_inputs to select input.value
    return empty_links


def _process_widget(type: Type, val):
    if isinstance(type, RangeType):
        # Val is a Tuple[float, float]
        # Convert to Range
        val = type.create(**val)
    elif isinstance(type, Slide):
        # Val needs to be validated
        val = type.create(val)

    return val


def _process_node_inputs(program, node: NodeN, ids):
    empty_links = _process_node_links(program, node, ids)

    # node.inputs : Dict[str, InputN]
    real_node = program.pipeline.nodes[node.id]

    for name in empty_links:
        type = real_node.func.InputTypes[name]
        # todo: will node.inputs[name].value ever be missing or invalid? if so, raise NodeImportError
        real_node.set_static_link(name, _process_widget(type, node.inputs[name].value))


def _process_node_settings(program, node: NodeN):
    if None in node.settings.values():
        raise NodeTreeImportError(
            program, node, "Cannot have None value in settings", exc_info=False
        )

    real_node = program.pipeline.nodes[node.id]

    settings = {}

    try:
        for field in real_node.func_type.SettingTypes:
            if field.type is None:
                # field is disabled
                continue

            # throws ValueError on invalid
            # try the following before giving up:
            # 1. Use the value provided by the nodetree
            # 2. If none provided by nodetree, get default value for element in dataclass
            # 3. If no default, try default initializing the type
            # 4. If can't default initialize type, give up
            try:
                setting = _process_widget(field.type, node.settings[field.name])
            except KeyError:
                default = {
                    x.name: x.default for x in fields(real_node.func_type.Settings)
                }[field.name]
                if default is not None:
                    setting = default
                else:
                    blank = type(field.type)
                    if blank is not None:
                        setting = blank
                    else:
                        raise ValueError(f"Cannot default initialize type {type}")
            settings[field.name] = setting

        # throws TypeError on missing
        settings = real_node.func_type.Settings(**settings)

        # throws ValueError on invalid
        settings = real_node.func_type.validate_settings(settings)

    except (KeyError, TypeError) as e:
        raise NodeTreeImportError(program, node, "Missing key in settings") from e

    except ValueError as e:
        raise NodeTreeImportError(program, node, "Invalid settings") from e

    if (
        (real_node.func_type.require_restart)  # restart only on changed settings
        and (real_node.settings is not None)
        and (not real_node.settings == settings)
    ) or real_node.func.always_restart:  # or if force always
        real_node.dispose()

    if real_node.func:
        real_node.func.settings = settings

    real_node.settings = settings


def _remove_unneeded_nodes(program, nodetree: NodeTreeN) -> Tuple[NodeTreeN, bool]:
    visited = set()
    broken = set()
    queue = deque()
    nodes = {}

    # First, add all sideeffect nodes to queue
    for node in nodetree.nodes:
        nodes[node.id] = node

        try:
            func = program.manager.funcs[node.type]
            if func.has_sideeffect:
                queue.append(node.id)
        except KeyError:
            # if function doesn't exist
            broken.add(node.id)

    # If any broken nodes, don't remove any except broken
    # We can't be sure which may have been side effect nodes
    # Remove the outputs of broken nodes
    if broken:
        nodes = [node for node in nodetree.nodes if node.id not in broken]
        # slow and messy but temporary™
        for node in broken:
            for sub in nodes:
                remove = []
                for name, val in sub.inputs.items():
                    if val.id == node:
                        remove.append(name)
                for name in remove:
                    del sub.inputs[name]
        nodetree = nodetree.copy(update={"nodes": nodes})
        # return nodetree and report that there are broken nodes
        return nodetree, True

    # Then, do a DFS over queue, adding all reachable nodes to visited
    while queue:
        id = queue.pop()

        if id in visited:
            continue

        visited.add(id)

        for input in nodes[id].inputs.values():
            link = input.link

            if link is None:
                continue

            queue.append(link.id)

    # Finally, remove those nodes that weren't visited
    nodes = [node for node in nodetree.nodes if node.id in visited]

    # make a copy of nodetree to fix broken json save
    nodetree = nodetree.copy(update={"nodes": nodes})

    # return nodetree and report no broken nodes
    return nodetree, False


def import_nodetree(program, nodetree: NodeTreeN, force_save: bool = False):
    original_nodetree = nodetree
    nodetree, broken = _remove_unneeded_nodes(program, nodetree)
    ids = [node.id for node in nodetree.nodes]

    # TODO : how to cache FifoLock in the stateless import_nodetree function?
    with FifoLock(program.queue):
        program.pipeline.prune_nodetree(ids)

        for node in nodetree.nodes:
            if node.id not in program.pipeline.nodes:
                try:
                    program.create_node(node.type, node.id)
                except KeyError as e:
                    raise NodeTreeImportError from e

        if not broken:
            for node in nodetree.nodes:
                try:
                    _process_node_settings(program, node)
                    _process_node_inputs(program, node, ids)
                except Exception as e:
                    if not force_save:
                        raise e

                try:
                    program.pipeline.nodes[node.id].ensure_init()
                except Exception as e:
                    try:
                        del program.pipeline.nodes[node.id]
                    except KeyError:
                        pass

                    if not force_save:
                        raise NodeTreeImportError(
                            program, node, "Error creating Function"
                        ) from e

        try:
            program.pipeline.run()
            program.manager.pipeline_update()
        except Exception:
            program.pipeline.broken = True
            if force_save:
                program.lifespan.persist.nodetree = original_nodetree
            raise NodeTreeImportError(
                program,
                program.pipeline.current,
                "Failed test run due to",
                real_node=True,
            )

        program.lifespan.persist.nodetree = original_nodetree
        program.pipeline.broken = False
