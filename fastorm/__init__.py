#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""FastORM framework, easy to learn, fast to code"""
__author__ = 'luckydonald'
__version__ = "0.0.17"
__all__ = [
    # meta:
    '__author__', '__version__',
    # classes:
    'FastORM', 'Autoincrement',
    # other modules:
    'query', 'utils', 'classes', 'compat',
]

import ipaddress
import builtins
import datetime
import asyncpg
import decimal
import typing
import types
import uuid
import re
from typing import List, Dict, Any, Optional, Tuple, Type, Union, TypeVar, Callable, Set
from datetime import timezone

try:
    import psycopg2
    import psycopg2.sql
    import psycopg2.extensions
except (ImportError, ModuleNotFoundError):
    psycopg2 = None
# end try

from luckydonaldUtils.exceptions import assert_type_or_raise
from luckydonaldUtils.logger import logging
from luckydonaldUtils.typing import JSONType

try:
    from pydantic.main import ModelMetaclass
    from pydantic.fields import ModelField, UndefinedType, Undefined, Field, PrivateAttr
    from pydantic.typing import NoArgAnyCallable, resolve_annotations
except ImportError:
    from pydantic.v1 import BaseModel
    from pydantic.v1.main import ModelMetaclass
    # from pydantic._internal._model_construction import ModelMetaclass
    from pydantic.v1.fields import ModelField, UndefinedType, Undefined, Field, PrivateAttr
    from pydantic.v1.typing import NoArgAnyCallable, resolve_annotations
# end try
from typeguard import check_type

from asyncpg import Connection, Pool, Record

from .classes import FieldInfo, FieldItem, SqlFieldMeta
from .compat import check_is_new_union_type, TYPEHINT_TYPE, check_is_generic_alias, check_is_annotated_type, check_is_typing_union_type
from .compat import IS_MIN_PYTHON_3_9
from .compat import Annotated, NoneType
from .utils import failsafe_issubclass, evaluate_forward_ref, snakecase
from .query import *
from .query import __all__ as __query__all__
__all__.extend(__query__all__)


VERBOSE_SQL_LOG = True
SQL_DO_NOTHING = "SELECT 1;"

CLS_TYPE = TypeVar("CLS_TYPE")

UTC = timezone.utc


logger = logging.getLogger(__name__)
if __name__ == '__main__':
    logging.add_colored_handler(level=logging.DEBUG)
# end if


class ModelMetaclassFastORM(ModelMetaclass):
    """
    Extend the normal pydantic Metaclass with some processing of other types.
    If we have references to other classes, we can additionally allow the Tuple form of their primary keys.

    so
    - `field: SomeTable` with primary key of `SomeTable` being `id: int` would become `field: Union[SomeTable, int]`
    - `field: SomeTable` with primary key of `SomeTable` being `id_a: int, id_b: float` would become `field: Union[SomeTable, Tuple[int, float]`
    """
    def __new__(mcs, name, bases, namespace, **kwargs):  # noqa C901
        logger.debug(f'name: {name!r}')
        logger.debug(f'bases: {bases!r}')
        logger.debug(f'kwargs: {kwargs!r}')
        logger.debug(f'namespace (old): {namespace!r}')
        all_automatic_fields = []

        collected_attributes = mcs.merge_base_class_attributes(
            bases=bases,
            namespace=namespace,
            dict_attributes=['__annotations__'],
            list_attributes=['_automatic_fields', '_primary_keys'],
        )

        all_annotations = collected_attributes.get('__annotations__', {})
        if all_annotations:
            all_new_annotations = mcs.process_annotation(all_automatic_fields, all_annotations)
        else:
            all_new_annotations = {}
        # end if

        for key in ('__annotations__', '_automatic_fields', '_primary_keys'):
            # TODO: Skip FastORM and _BaseFastORM  # Problem: hot to check? Those aren't created yet.
            if key in namespace:
                namespace[f'__original{key}'] = namespace[key]
                del namespace[key]  # so those two fields are inserted after each other
            # end if
            namespace[key] = collected_attributes[key]
        # end for
        namespace['__annotations__'] = all_new_annotations

        logger.debug(f'namespace (new): {namespace!r}')
        cls = super().__new__(mcs, name, bases, namespace, **kwargs)
        cls.__original__fields__ = mcs.process_fields(
            generated_new_fields=cls.__fields__,
            new_annotations=all_new_annotations,
            original_annotations=all_annotations,
            namespace=namespace,
        )
        return cls
    # end def

    @classmethod
    def merge_base_class_attributes(
        mcs,
        bases: List[type],
        namespace: Dict[str, Any],
        dict_attributes: List[str],
        list_attributes: List[str],
    ) -> Dict[str, Union[List, Dict[str, Any]]]:
        """
        Goes through the needed fields (`dict_attributes` and `list_attributes` in the base classes (`bases`),
        as well at the current `namespace`, and collects them to a dict.
        The key is the field name as in those `*_attributes` list, the value is a merged `dict` or `list` as indicated
        by the `*_attributes` list you used.

        :param bases: The base classes of this function, where we search for those attributes.
        :param namespace: The current class which also may have those attributes.
        :param dict_attributes: Keys of the attributes to collect, those merged as a dict.
        :param list_attributes: Keys of the attributes to collect, those merged as a list.

        :return: Dictionary of the collected items. The keys are as in the provided attributes lists
                 and the values are the combined dicts or lists respectively.
        """
        return_value: Dict[str, Union[List, Dict[str, Any]]] = {}
        for key in dict_attributes:
            return_value[key] = {}
        # end for
        for key in list_attributes:
            return_value[key] = []
        # end for
        for base_cls in reversed(bases):  # TODO: Is reverse order correct?
            # `class C(A, B): pass` will keep A's variables over B's.
            # So we should update the dict of C with B first, to then overwrite with A.
            for key in dict_attributes:
                if hasattr(base_cls, key):
                    return_value[key].update(getattr(base_cls, key))
                # end if
            # end for

            for key in list_attributes:
                if hasattr(base_cls, key):
                    return_value[key].extend(getattr(base_cls, key))
                # end if
            # end for
        # end for

        # Additionally, process the currently to-be-added ones of this very class we are creating

        for key in dict_attributes:
            if key in namespace:
                return_value[key].update(namespace[key])
            # end if
        # end for

        for key in list_attributes:
            if key in namespace:
                return_value[key].extend(namespace[key])
            # end if
        # end for
        return return_value
    # end def

    @classmethod
    def process_annotation(mcs, automatic_fields: List[str], annotations: Dict[str, TYPEHINT_TYPE]):
        """
        Makes sure that primary keys
        :param automatic_fields:
        :param annotations:
        :return:
        """
        new_annotations = {}
        for field_name, annotation in annotations.items():
            annotation_args = mcs.recursive_get_union_params_with_pk_types(annotation)
            if field_name in automatic_fields:
                annotation_args.append(None)
            # end if
            new_annotations[field_name] = Union.__getitem__(tuple(annotation_args))  # calls Union[…]
        # end for
        return new_annotations
    # end def

    @classmethod
    def recursive_get_union_params_with_pk_types(mcs, annotation):
        """
        Processes the annotation to check if there's a reference to another FastORM table in there, and if so,
        will replace that with a tuple of that and the other table primary keys.

        That way, if you're referenced table has a single integer, when working with data,
        you can set it to that single integer of the primary key instead of providing a fully loaded object
        which has those primary key values set. But that's still possible, too.
        :param annotation:
        :return:
        """
        # is a Union[…]
        is_union = check_is_typing_union_type(annotation) or check_is_new_union_type(annotation)
        # is a List[…], Dict[…], ...
        is_complex = check_is_generic_alias(annotation)

        if annotation == typing.Any:  # do not attempt to upgrade `Any` in any way.
            annotation_args = [annotation]
        elif is_union or is_complex:
            assert hasattr(annotation, '__args__')
            annotation_args = []
            if is_union:
                for arg in annotation.__args__:
                    annotation_args.extend(mcs.recursive_get_union_params_with_pk_types(arg))
                # end for
            else:
                assert is_complex
                # stuff = […] -> Dict[*stuff] -> [Dict[…]]
                for arg in annotation.__args__:
                    param = mcs.recursive_get_union_params_with_pk_types(arg)
                    if len(param) == 1:
                        param = param[0]
                    else:
                        assert len(param) > 0
                        # Dict[str, Table] -> Dict[str, Union[str, table_pks]]
                        param = Union.__getitem__(tuple(param))  # calls Union[…]
                    # end if
                    annotation_args.append(param)
                # end for
                annotation_args = tuple(annotation_args)
                if annotation_args:
                    # check if is a `Callable[[param1, ...], return_type]`
                    # as they need a special packing of the parameters:
                    # A tuple, where the last element of the `annotation_args` tuple element is the return type,
                    # and everything before that is the type of each function parameter.
                    # Then merged to a single tuple with two items: list of parameter types, return type.
                    #
                    # We try checking out `annotation._name`, and if that fails the `repr(…)`.
                    # Same as isinstance(annotation, _CallableGenericAlias), but without that funky import.
                    is_callable = False
                    if hasattr(annotation, '_name'):
                       is_callable = getattr(annotation, '_name') == 'Callable'
                    else:
                        is_callable = (
                          repr(annotation) == 'typing.Callable' or repr(annotation).startswith('typing.Callable[') or
                          repr(annotation) == 'collections.abc.Callable' or repr(annotation).startswith('collections.abc.Callable[')
                        )
                    # end if
                    if is_callable:
                        # Basically taken from the __repr__ function of typing._CallableGenericAlias:
                        # f'typing.Callable[[{", ".join([_type_repr(a) for a in args[:-1]])}], {_type_repr(args[-1])}]'
                        annotation_args = (list(annotation_args[:-1]), annotation_args[-1])
                    # end if
                # end if

                if hasattr(annotation.__origin__, '__gettiem__'):
                    annotation_args = getattr(annotation.__origin__, '__gettiem__')(annotation_args)
                else:
                    annotation_args = annotation.__origin__[annotation_args]  # calls list[…]
                # end if
                annotation_args = [annotation_args]
            # end if
        else:
            annotation_args = mcs.upgrade_reference_annotation(annotation)
        # end if
        annotation_args = mcs.deduplicate_types(annotation_args)  # especially tuple vs typing.Tuple
        return annotation_args
    # end def

    @classmethod
    def upgrade_reference_annotation(mcs, annotation: TYPEHINT_TYPE) -> List[TYPEHINT_TYPE]:
        """
        Upgrades a reference annotation to another FastORM table to be a Tuple of that class and their primary keys.

        :used-by: ModelMetaclassFastORM.recursive_get_union_params_with_pk_types()
        :param annotation:
        :return:
        """
        annotations = [annotation]
        try:
            if isinstance(annotation, typing.ForwardRef) and not annotation.__forward_evaluated__:
                logger.warning(
                    f'The typehint of that class still contains a unresolved ForwardRef({annotation.__forward_arg__!r}). You should probably call Class.update_forward_refs() after the class it is pointing to is defined.'
                )
            elif isinstance(annotation, typing.ForwardRef):  # __forward_evaluated__ now True
                annotation = annotation.__forward_value__
            elif issubclass(annotation, _BaseFastORM):
                pk_annotations = tuple(annotation.get_primary_keys_type_annotations().values())
                if len(pk_annotations) == 1:
                    pk_annotations = pk_annotations[0]
                else:
                    # noinspection PyArgumentList
                    pk_annotations = Tuple.__getitem__(pk_annotations)
                # end if
                annotations.append(pk_annotations)
            # end if
        except TypeError as e:
            # TypeError: issubclass() arg 1 must be a class
            logger.warn(f'Annotation failed to process: {annotation}', exc_info=True)
            pass
        return annotations
    # end def

    @classmethod
    def process_fields(
        mcs,
        generated_new_fields: Dict[str, ModelField],
        new_annotations: Dict[str, TYPEHINT_TYPE],
        original_annotations: Dict[str, TYPEHINT_TYPE],
        namespace: Dict
    ) -> Dict[str, ModelField]:
        """
        Compare `original_annotations` and `new_annotations`.
        For annotations that differ we have to build a new pydantic representation for the new `__fields__`.
        as they need to be based on the new `__annotations__`.
        Otherwise, we can be cheap and simply copy the old ones over.
        """
        retrofitted_fields: Dict[str, ModelField] = {}
        annotations = None  # basically a cache, we don't wanna resolve annotations over and over again
        for key in generated_new_fields.keys():
            # prepare if equal
            new_annotation = new_annotations[key]
            original_annotation = original_annotations[key]
            is_equal = id(new_annotation) == id(original_annotation)
            # basically we wanna check `new_annotation == original_annotation`,
            #
            # BUT in some cases that can lead to INFINITE RECURSION:
            # If for some reason the resolved version of the `ForwardRef` points to itself (Why?),
            # in other words `id(ref) == id(ref.__forward_value__)`,
            # in `ForwardRef.__eq__(…)` it will compare it's `__forward_value__` with the `__forward_value__` of the other element.
            # As that's the same as we started with, we're trapped in an infinite loop.
            # We actually handle that case.
            #
            # I have not yet figured out why that happens in the first place, as `x = ForwardRef('x'); x == x` does work,
            # but the test_create_table unittest's TableWithForwardRef is causing that behaviour.
            # This can/could be seen e.g. in the version of commit 4ffd7e3964d339483e195adcceaa2d6fa2a70f54.
            if not is_equal:  # the id(…) comparison is not enough
                is_forward_refs = isinstance(new_annotation, typing.ForwardRef), isinstance(original_annotation, typing.ForwardRef)
                if is_forward_refs == (True, True):
                    # both are ForwardRefs. This is where this delicate problem can occur.
                    new_annotation: typing.ForwardRef
                    original_annotation: typing.ForwardRef
                    is_equal = is_equal or (
                        id(new_annotation.__forward_evaluated__) == id(new_annotation)  # will make a recursive loop
                        and
                        id(original_annotation.__forward_evaluated__) == id(original_annotation)  # will make a recursive loop
                        and
                        new_annotation.__forward_arg__ == new_annotation.__forward_arg__  # the unresolved string will be enough
                    )
                elif is_forward_refs == (True, False):
                    # only one is a forward ref -> check the actual value with the forwarded one
                    is_equal = is_equal or (new_annotation.__forward_evaluated__ and new_annotation.__forward_value__ == original_annotation)
                elif is_forward_refs == (False, True):
                    # only one is a forward ref -> check the actual value with the forwarded one
                    is_equal = is_equal or (original_annotation.__forward_evaluated__ and original_annotation.__forward_value__ == new_annotation)
                # end if

                # if it's not yet considered equal, we're gonna resort to normal comparison, hopefully no recursion now.
                is_equal = is_equal or new_annotation == original_annotation
            # end if

            if is_equal:
                # we have no change in types, so we can easily skip and just use the same as the generated one.
                retrofitted_fields[key] = generated_new_fields[key]
                continue
            # end if

            # now the tough part, mimicking pydantic's processing
            if annotations is None:
                annotations = resolve_annotations(original_annotations, namespace.get('__module__', None))  # TODO: shouldn't this be the new annotations?!?
            # end if

            # annotation only fields need to come first in fields (???)
            value = namespace.get(key, Undefined)
            ann_type = annotations[key]
            # basically we copy over `.class_validators` and `.model_config`,
            # pull the ann_type from the `original_annotations` as resolved by pydantic,
            # and that allows for a duplicated `__field__` list.
            retrofitted_fields[key] = ModelField.infer(
                name=key,
                value=value,
                annotation=ann_type,
                class_validators=generated_new_fields[key].class_validators,  # TODO? vg.get_validators(ann_name) ?
                config=generated_new_fields[key].model_config,
            )
        # end for
        return retrofitted_fields
    # end def

    @classmethod
    def deduplicate_types(mcs, annotations: List[TYPEHINT_TYPE]) -> List[TYPEHINT_TYPE]:
        """
        Makes sure that `[int, int, float, int]` is deduplicated to `[int, float]`.
        And `[int, int, int, int]` becomes a single [`int`].

        This a Union can be deduplicated:
        `Union[int, int, float, int]` becomes `Union[int, float]`.
        This is exactly what's happening in `recursive_get_union_params_with_pk_types()`.


        :used-by: ModelMetaclassFastORM.recursive_get_union_params_with_pk_types()
        :param annotations:
        :return:
        """
        # first barebones deduplication
        if not IS_MIN_PYTHON_3_9:
            # old python version: we don't have `tuple[…]` which we would need to distinguish from `typing.Tuple[…]`,
            # so we can optimize with letting set do the deduplication, and only run the list based compare if that
            # resulting set is shorter.
            params_set = set(annotations)
            if len(params_set) == len(annotations) and not IS_MIN_PYTHON_3_9:
                return annotations
            # end if
        # end if

        # this either did yield a shorter list (which we now need to reproduce while keeping element order),
        # or that we're python 3.9+ where there could be `tuple[…]` which would not be equal to `typing.Tuple[…]` for that.
        all_params = list()
        generic_aliases = set()

        for param in annotations:
            if IS_MIN_PYTHON_3_9 and check_is_generic_alias(param):
                # check if we have a different type with similar stuff.
                # so tuple[] instead of Tuple[] and so on.
                assert hasattr(param, '__args__')
                assert hasattr(param, '__origin__')

                # Tuple[int, str] == tuple[int, str]  # False
                # but:
                # Tuple[int, str] == Tuple[int, str]  # True
                # tuple[int, str] == tuple[int, str]  # True
                # Tuple[int, str].__args__ ==  tuple[int, str].__args__ == (int, str)  # True
                # Tuple[int, str].__origin__ == tuple[int, str].__origin__ == tuple  # True
                is_duplicate = any(mcs.is_generic_alias_equal(param, other_param) for other_param in generic_aliases)
                if is_duplicate:
                    continue
                # end if
                generic_aliases.add(param)
            # end if
            all_params.append(param)
        # end for
        return list(all_params)
    # end def

    @staticmethod
    def is_generic_alias_equal(param, other_param):
        """
        A comparison function for `GenericAlias`es (as determined by `check_is_generic_alias()`).
        Returns true on direct equality,
        or if both are a `GenericAlias` and have the same `__args__` and `__origin__`.

        :param param:
        :param other_param:
        :return:
        """
        if param == other_param:
            return True
        # end def
        if not check_is_generic_alias(param) or not hasattr(param, '__args__') or not hasattr(param, '__origin__'):
            return False
        # end def
        if not check_is_generic_alias(other_param) or not hasattr(other_param, '__args__') or not hasattr(other_param, '__origin__'):
            return False
        # end def
        return param.__args__ == other_param.__args__ and param.__origin__ == other_param.__origin__
    # end def
# end class


class _BaseFastORM(BaseModel):
    _table_name: str  # database table name we run queries against
    _ignored_fields: List[str]  # fields which never are intended for the database and will be excluded in every operation. (So are all fields starting with an underscore)
    _automatic_fields: List[str]  # fields the database fills in, so we will ignore them on INSERT.
    _primary_keys: List[str]  # this is how we identify ourself.
    _database_cache: Dict[str, JSONType] = PrivateAttr()  # stores the last known retrieval, so we can run UPDATES after you changed parameters.
    __selectable_fields: List[str] = PrivateAttr()  # cache for `cls.get_sql_fields()`
    __fields_typehints: Dict[bool, Dict[str, FieldInfo[ModelField]]] = PrivateAttr()  # cache for `cls.get_fields_typehint()`
    __fields_references: Dict[bool, Dict[str, FieldInfo[ModelField]]] = PrivateAttr()  # cache for `cls.get_fields_typehint()`
    __original__annotations__: Dict[str, Any]  # filled by the metaclass, before we do modify the __annotations__
    __original__fields__: Dict[str, ModelField]  # filled by the metaclass, before we do modify the __fields__

    def __init__(self, **data: Any):
        super().__init__(**data)
        self._database_cache: Dict[str, Any] = {}
    # end def

    def _database_cache_overwrite_with_current(self):
        """
        Resets the database cache from the current existing fields.
        This is used just after something is loaded from a database row.
        :return:
        """
        self._database_cache = {}
        for field in self.get_fields():
            self._database_cache[field] = getattr(self, field)
        # end if

    def _database_cache_remove(self):
        """
        Removes the database cache completely.
        This is used after deleting an entry.
        :return:
        """
        self._database_cache = {}
    # end def

    def as_dict(self) -> Dict[str, JSONType]:
        return self.dict()
    # end def

    @classmethod
    def get_fields_typehints(cls, *, flatten_table_references: bool = False) -> Dict[str, FieldInfo[ModelField]]:
        """
        Get's all fields which have type hints and thus we consider as fields for the database.
        Filters out constants (all upper case, like `CAPSLOCK_VARIABLE`) and hidden fields (starting with `_`).

        :uses: FastORM.get_fields_references

        :param flatten_table_references:
                True if we should flatten the references to other table's primary key in the format of `f"{original_key}__{other_table_key}`.
                False to not resolve those fields, and instead return the type hint for the other FastORM class.
        :return: the dictionary with pydantic's ModelField descriptions.

        Example:

            >>> class OtherTable(FastORM):
            ...     _table_name = 'other_table'
            ...     _primary_keys = ['id_part_1', 'id_part_2']
            ...
            ...     id_part_1: int
            ...     id_part_2: str
            ... # end class
            ...

            >>> class ActualTable(FastORM):
            ...     _table_name = 'actual_table'
            ...     _primary_keys = ['cool_reference']
            ...     cool_reference: OtherTable
            ...

            >>> ActualTable.get_fields_typehints(flatten_table_references=False)
            {'cool_reference': FieldInfo(is_primary_key=True, types=[FieldItem(field='cool_reference', type_=ModelField(name='cool_reference', type=OtherTable, required=True))])}


            >>> ActualTable.get_fields_typehints(flatten_table_references=True)
            {'cool_reference__id_part_1': FieldInfo(is_primary_key=True, types=[FieldItem(field='cool_reference', type_=ModelField(name='cool_reference', type=OtherTable, required=True)), FieldItem(field='id_part_1', type_=ModelField(name='id_part_1', type=int, required=True))]), 'cool_reference__id_part_2': FieldInfo(is_primary_key=True, types=[FieldItem(field='cool_reference', type_=ModelField(name='cool_reference', type=OtherTable, required=True)), FieldItem(field='id_part_2', type_=ModelField(name='id_part_2', type=str, required=True))])}

        """
        # first look if we have this cached. That would be way faster.
        cache_key = f'_{cls.__name__!s}__fields_typehints'
        cached_value = getattr(cls, cache_key, {})
        # it is a dict, where the boolean key is the `flatten_table_references` parameter.
        if cached_value.get(flatten_table_references) is not None:
            return cached_value[flatten_table_references]
        # end if

        # we don't have a cached value and need to calculate it all.
        # that's fair, let's do it.

        _ignored_fields = cls.get_ignored_fields()
        references = cls.get_fields_references(recursive=flatten_table_references)

        # prepare the array with all the classes we wanna have typehints for.
        classes_typehints: Dict[Type, Dict[str, ModelField]] = {
            cls: {},
        }
        for long_key, field_reference in references.items():
            for interesting_type in field_reference.types[:-1]:  # the last one always is a native type (int, str, …), so not interesting
                classes_typehints[interesting_type.type_] = {}
            # end for
        # end for

        # now actually fill in type hints to that lookup table.
        for interesting_cls in classes_typehints.keys():
            interesting_cls: Type[FastORM]
            assert issubclass(interesting_cls, FastORM)
            type_hints: Dict[str, ModelField] = {
                key: value for key, value in interesting_cls.__original__fields__.items()
                if (
                    not key.startswith('_')
                    and not key.isupper()
                    and key not in _ignored_fields
                )
            }
            classes_typehints[interesting_cls] = type_hints
        # end for

        # now go through the references and apply the type hints
        result_hints: Dict[str, FieldInfo[ModelField]] = {}
        for long_key, field_reference in references.items():
            last_class = cls
            final_hint = FieldInfo(is_primary_key=field_reference.is_primary_key, types=[])
            for field_item in field_reference.types:
                type_hint = classes_typehints[last_class][field_item.field]
                if isinstance(type_hint.outer_type_, typing.ForwardRef):
                    new_type = type_hint.type_
                    if not type_hint.outer_type_.__forward_evaluated__ and type_hint.outer_type_.__forward_arg__ == cls.__name__:
                        # special case where the class points to itself, recursively
                        # force it to update its forward ref with itself.
                        # for some reason `cls.update_forward_refs()` doesn't do that reliable,
                        # but as we have our own class ready, we can do it easily here.
                        type_hint.outer_type_._evaluate(globalns={}, localns={cls.__name__: cls}, recursive_guard=frozenset())
                        new_type = type_hint.outer_type_.__forward_value__
                    # end if
                    # so we check if that resolving was successful, or the user manually resolved it beforehand.
                    if not type_hint.outer_type_.__forward_evaluated__:
                        raise AssertionError(f'Unevaluated ForwardRef. Try to call {cls.__name__}.update_forward_refs() after the referenced class ({type_hint.outer_type_.__forward_arg__}) it defined.')
                    # end if
                    if new_type == type_hint.type_:
                        logger.debug('Case with the unions?')
                    # end if
                    # The .type_ of a resolved ForwardRef seems alright, only the Optional[…] wrapping goes poof.
                    # We parse the inner resolved .type_ as a new hint (supplying Optional[…] where needed),
                    # and if the inner parsed .type_ still matches, we replace the current type hint by this new one.
                    new_type_hint = ModelField(
                        name=type_hint.name,
                        type_=new_type if type_hint.allow_none else Optional[new_type],
                        model_config=type_hint.model_config,
                        default=type_hint.default,
                        default_factory=type_hint.default_factory,
                        required=type_hint.required,
                        alias=type_hint.alias,
                        field_info=type_hint.field_info,
                        class_validators=type_hint.class_validators,
                    )
                    if (
                        new_type_hint.type_ == new_type and
                        ((type_hint.required == Undefined and new_type_hint.required is False) or (new_type_hint.required == type_hint.required)) and
                        new_type_hint.outer_type_ != type_hint.outer_type_ and
                        True
                    ):
                        type_hint = new_type_hint
                    else:
                        raise AssertionError('O_O this should not happen')
                    # end if
                # end if
                final_hint.types.append(FieldItem(field=field_item.field, type_=type_hint))
                last_class = field_item.type_
            # end for
            result_hints[long_key] = final_hint
        # end for
        cached_value[flatten_table_references] = result_hints
        setattr(cls, cache_key, cached_value)
        return result_hints
    # end def

    _GET_FIELDS_REFERENCES_TYPE = Dict[str, FieldInfo[typing.Union[type, typing.Type['FastORM']]]]

    @classmethod
    def get_fields_references(cls, *, recursive: bool = False) -> _GET_FIELDS_REFERENCES_TYPE:
        """
        Get's all fields which have type hints and thus we consider as fields for the database.
        Filters out constants (all upper case, like `CAPSLOCK_VARIABLE`) and hidden fields (starting with `_`).

        :uses: FastORM.__original__fields__

        :param recursive: If we should not only take into account the current layer, but add the primary keys of the referenced tables to it as well, in the format `f"{our_key}__{referenced_primary_key}`.
        :return: the dictionary with the

        Example:

            >>> class OtherTable(FastORM):
            ...     _table_name = 'other_table'
            ...     _primary_keys = ['id_part_1', 'id_part_2']
            ...
            ...     id_part_1: int
            ...     id_part_2: str
            ... # end class
            ...

            >>> class ActualTable(FastORM):
            ...     _table_name = 'actual_table'
            ...     _primary_keys = ['cool_reference']
            ...     cool_reference: OtherTable
            ...     foobar: int
            ...

            >>> class ThirdTable(FastORM):
            ...     _table_name = 'third_table'
            ...     _primary_keys = ['id']
            ...     id: int
            ...     reference_to_other_table: OtherTable
            ...     reference_to_actual_table: ActualTable
            ...

            >>> class TableWithIdAndRefPK(FastORM):
            ...     _table_name = 'mayhem_group_tables'
            ...     _primary_keys = ['id', 'reference']
            ...     _automatic_fields = ['id']
            ...     id: int
            ...     reference: OtherTable
            ...

            >>> class TableWithIdAndRefPK(FastORM):
            ...     _table_name = 'mayhem_group_tables'
            ...     _primary_keys = ['id', 'reference']
            ...     _automatic_fields = ['id']
            ...     reference: Union[OtherTable, Tuple]
            ...


            >>> ActualTable.get_fields_references(recursive=False)
            {'cool_reference': FieldInfo(is_primary_key=True, types=[FieldItem(field='cool_reference', type_=<class 'fastorm.OtherTable'>)]), 'foobar': FieldInfo(is_primary_key=False, types=[FieldItem(field='foobar', type_=<class 'int'>)])}

            >>> ThirdTable.get_fields_references(recursive=False)
            {'id': FieldInfo(is_primary_key=True, types=[FieldItem(field='id', type_=<class 'int'>)]), 'reference_to_other_table': FieldInfo(is_primary_key=False, types=[FieldItem(field='reference_to_other_table', type_=<class 'fastorm.OtherTable'>)]), 'reference_to_actual_table': FieldInfo(is_primary_key=False, types=[FieldItem(field='reference_to_actual_table', type_=<class 'fastorm.ActualTable'>)])}

            >>> ThirdTable.get_fields_references(recursive=True)
            {'id': FieldInfo(is_primary_key=True, types=[FieldItem(field='id', type_=<class 'int'>)]), 'reference_to_other_table__id_part_1': FieldInfo(is_primary_key=False, types=[FieldItem(field='reference_to_other_table', type_=<class 'fastorm.OtherTable'>), FieldItem(field='id_part_1', type_=<class 'int'>)]), 'reference_to_other_table__id_part_2': FieldInfo(is_primary_key=False, types=[FieldItem(field='reference_to_other_table', type_=<class 'fastorm.OtherTable'>), FieldItem(field='id_part_2', type_=<class 'str'>)]), 'reference_to_actual_table__cool_reference__id_part_1': FieldInfo(is_primary_key=False, types=[FieldItem(field='reference_to_actual_table', type_=<class 'fastorm.ActualTable'>), FieldItem(field='cool_reference', type_=<class 'fastorm.OtherTable'>), FieldItem(field='id_part_1', type_=<class 'int'>)]), 'reference_to_actual_table__cool_reference__id_part_2': FieldInfo(is_primary_key=False, types=[FieldItem(field='reference_to_actual_table', type_=<class 'fastorm.ActualTable'>), FieldItem(field='cool_reference', type_=<class 'fastorm.OtherTable'>), FieldItem(field='id_part_2', type_=<class 'str'>)])}

        """
        # first look if we have this cached. That would be way faster.
        cache_key = f'_{cls.__name__!s}__fields_references'
        cached_value = getattr(cls, cache_key, {})
        # it is a dict, where the boolean key is the `recursive` parameter.
        if cached_value.get(recursive) is not None:
            return cached_value[recursive]
        # end if

        # we don't have a cached value and need to calculate it all.
        # that's fair, let's do it.

        _ignored_fields = cls.get_ignored_fields()
        _primary_keys = cls.get_primary_keys_keys()
        # copy the type hints as we might add more type hints for the primary key fields of referenced models, and we wanna filter.
        type_hints = {
            key: value for key, value in cls.__original__fields__.items()
            if (
                not key.startswith('_')
                and not key.isupper()
                and key not in _ignored_fields
            )
        }
        if cls.__original__fields__ != cls.__fields__:
            logger.warn(f'{cls.__name__}.__original__fields__ != {cls.__name__}.__fields__')
            try:
                logger.warn(f'{cls.__name__}.__original__fields__ = {cls.__original__fields__!r}')
            except AttributeError as e:
                logger.warn(f'{cls.__name__}.__original__fields__ = {e!s}')
            # end try
            try:
                logger.warn(f'{cls.__name__}.__fields__ = {cls.__fields__!r}')
            except AttributeError as e:
                logger.warn(f'{cls.__name__}.__fields__ = {e!s}')
            # end try
            logger.warn('.')
        # end if
        return_val: FastORM._GET_FIELDS_REFERENCES_TYPE = {}
        for key, value in type_hints.items():
            type_hint = type_hints[key]
            inner_type = type_hint.type_
            other_class: Union[Type[FastORM], None] = None
            if (
                check_is_generic_alias(inner_type) and
                hasattr(inner_type, '__origin__') and
                type_hint.type_.__origin__ == typing.Union
            ):  # Union
                # it's a Union
                union_params_unclean = type_hint.type_.__args__[:]
                union_params = []
                for union_param in union_params_unclean:
                    union_param = evaluate_forward_ref(union_param, key=key, cls_name=cls.__name__)
                    # We also need to handle NoneType in those once more,
                    # Usually that would be filtered converted to Optional[…],
                    # but apparently not for ForwardRefs.
                    if union_param == NoneType:
                        continue
                    # end if
                    union_params.append(union_param)
                # end for
                first_union_type = union_params[0]

                if issubclass(first_union_type, FastORM):
                    # we can have a reference to another Table, so it could be that
                    # the table ist the first entry and the actual field type is the second.
                    # Union[Table, int]
                    # Union[Table, Tuple[int, int]]
                    expected_typehint = list(first_union_type.get_primary_keys_type_annotations().values())
                    if not len(union_params) == 2:
                        raise TypeError(
                            f'Union with other table type must have it\'s primary key(s) as second argument, and no more values:\n'
                            f'Union{union_params!r}\n'
                            f'Expected instead:\n'
                            f'Union[{first_union_type}, {expected_typehint}]'
                        )
                    # end if
                    implied_other_class_pk_types = union_params[1]
                    if (
                        check_is_generic_alias(implied_other_class_pk_types) and
                        hasattr(implied_other_class_pk_types, '__origin__') and
                        implied_other_class_pk_types.__origin__ == tuple and
                        hasattr(implied_other_class_pk_types, '__args__') and
                        implied_other_class_pk_types.__args__
                    ):
                        implied_other_class_pk_types = list(implied_other_class_pk_types.__args__)
                    else:
                        implied_other_class_pk_types = [implied_other_class_pk_types]
                    # end if
                    typehints = first_union_type.get_fields_typehints(flatten_table_references=True)
                    pk_keys_actual_types = []
                    for hint in typehints.values():
                        if not hint.is_primary_key:
                            continue
                        # end if
                        type_ = cls.wrap_optional_pydantic_typehint(hint.resulting_type)
                        pk_keys_actual_types.append(type_)
                    # end for
                    if implied_other_class_pk_types == pk_keys_actual_types:
                        # so basically the we know the referenced(!) `SomeTable` has `_id = ['id']`,
                        # and the referenced `SomeTable.id` is of type `int`,
                        # and now our given type, which is referencing it, is `Union[SomeTable, int]`, that is matching that.

                        # in other words:
                        # A `SomeTable` with primary key  being `int`        needs to have the reference called either `SomeTable` or `Union[SomeTable, int]` (`Union[SomeTable, [int]]` is possible too)
                        # A `SomeTable` with primary keys being `[int, str]` needs to have the reference called either `SomeTable` or `Union[SomeTable, [int, str]]`
                        other_class = first_union_type  # okay, we can use Table even if there's more. That way we have normalized it and there won't be an error later.
                    # end if
                # end if
            # end if
            if not other_class:
                try:
                    if issubclass(type_hint.type_, FastORM):
                        other_class: Type[FastORM] = type_hint.type_
                    # end if
                except TypeError:
                    pass
                # end try
            # end if
            other_class: Union[Type[FastORM], None]
            if not other_class:
                # is a regular key, just keep it as is
                # e.g. 'title': (False, [('title', str)]),
                return_val[key] = FieldInfo(
                    is_primary_key=key in _primary_keys,
                    types=[FieldItem(key, cls.wrap_optional_pydantic_typehint(type_hint))]
                )  # TODO: make a copy?
                # and then let's do the next key
                continue
            # end if

            # now we know: it's another FastORM table definition.
            assert issubclass(other_class, FastORM)
            # 'test_two__test_one_a__id_part_1': (True, [('test_two', Test2), ('test_one_a', Test1A), ('id_part_1', int)]),
            if not recursive:
                return_val[key] = FieldInfo(
                    is_primary_key=key in _primary_keys,
                    types=[FieldItem(key, other_class)]
                )  # TODO: make a copy?
                continue
            # end if
            if other_class == cls:
                # Referencing ourselves in a loop.
                for self_reference_primary_key in _primary_keys:
                    return_val[f'{key}__{self_reference_primary_key}'] = FieldInfo(
                        is_primary_key=key in _primary_keys,
                        types=[FieldItem(key, other_class)] + return_val[self_reference_primary_key].types  # let's just hope we are done with our primary keys, as they should be ordered on top. Otherwise, this will fail.
                    )
                # end for
                continue
            # end if

            other_refs = other_class.get_fields_references(recursive=True)  # recursive must be True, as False has a `continue` above.

            for other_long_name, field_ref in other_refs.items():
                if not field_ref.is_primary_key:
                    continue
                # end if
                if not field_ref.types:
                    raise ValueError(f'Huh? No history at all! {other_long_name!r}, {other_class!r}, {field_ref.types!r}')
                # end if
                return_val[f'{key}__{other_long_name}'] = FieldInfo(
                    is_primary_key=key in _primary_keys,
                    types=[FieldItem(key, other_class)] + field_ref.types
                )
                # end if
            # end for
        # end for
        cached_value[recursive] = return_val
        setattr(cls, cache_key, cached_value)
        return return_val
    # end def

    @staticmethod
    def wrap_optional_pydantic_typehint(typehint: ModelField):
        """
        Wrap e.g. an optional `int` back as `Optional[int]`.
        :param typehint_:
        :return:
        """
        type_ = typehint.type_
        if typehint.allow_none:
            # wrap e.g. an optional `int` as `Optional[int]`.
            type_ = Optional.__getitem__(type_)
        # end if
        return type_
    # end def

    @classmethod
    def get_fields(cls, flatten_table_references: bool = False) -> List[str]:
        """
        Get's all fields which have type hints and thus we consider as fields for the database.
        Filters out constants (all upper case, like `CAPSLOCK_VARIABLE`) and hidden fields (starting with `_`).
        :return: a list with the keys.
        """
        return list(cls.get_fields_typehints(flatten_table_references=flatten_table_references).keys())
    # end def

    @classmethod
    def get_automatic_fields(cls) -> List[str]:
        _automatic_fields = getattr(cls, '_automatic_fields', [])[:]
        return _automatic_fields
    # end def

    @classmethod
    def get_ignored_fields(cls) -> List[str]:
        _ignored_fields = getattr(cls, '_ignored_fields', [])
        if isinstance(_ignored_fields, types.MemberDescriptorType):
            # basically that means it couldn't find any actually existing field
            _ignored_fields = []
        # end if
        _ignored_fields = [*_ignored_fields]  # make copy
        assert_type_or_raise(_ignored_fields, list, parameter_name=f'{cls.__name__}._ignored_fields')
        _ignored_fields += [
            '_table_name',
            '_ignored_fields',
            '_automatic_fields',
            '_primary_keys',
            '_database_cache',
            '__selectable_fields',
            '__fields_typehints',
            '__fields_references',
            f'_{cls.__name__!s}__selectable_fields',
            f'_{cls.__name__!s}__fields_typehints',
            f'_{cls.__name__!s}__fields_references',
            '__slots__'
        ]
        return _ignored_fields
    # end def

    @classmethod
    def get_sql_fields(cls) -> List[str]:
        key = f'_{cls.__name__!s}__selectable_fields'
        if getattr(cls, key, None) is None:
            setattr(cls, key, [field for field in cls.get_fields(flatten_table_references=True) if not field.startswith('_')])
        # end if
        return getattr(cls, key)
    # end if

    @classmethod
    def get_select_fields(cls, *, namespace=None) -> str:
        if namespace:
            return ', '.join([f'"{namespace}"."{field}" AS "{namespace} {field}"' for field in cls.get_sql_fields()])
        # end if
        return ', '.join([f'"{field}"' for field in cls.get_sql_fields()])
    # end def

    @classmethod
    def get_select_fields_len(cls) -> int:
        return len(cls.get_sql_fields())
    # end if

    @classmethod
    def get_table(cls) -> str:
        """
        Provides the name of the table in a already quoted way, ready to use in SQL queries.
        Note, that is naive quoting, and can be easily broken out of (possible SQL INJECTION), however the table name
        should never be user input anyway.

            >>> class Test(FastORM):
            ...   _table_name = 'sample'
            >>> Test.get_name()
            'sample'
            >>> Test.get_table()
            '"sample"'
            >>> print(Test.get_name())
            sample
            >>> print(Test.get_table())
            "sample"

        :return: The quoted table name
        """
        _table_name = cls.get_name()
        return f'"{_table_name}"'
    # end def

    @classmethod
    def get_name(cls) -> str:
        """
        The name of the table, as used with the database.
        :return: `table`
        """
        _table_name = getattr(cls, '_table_name', snakecase(cls.__name__))
        return _table_name
    # end def

    def build_sql_insert(
        self, *,
        ignore_setting_automatic_fields: Optional[bool] = None,
        upsert_on_conflict: Union[List[str], bool] = False,
    ) -> Tuple[Any, ...]:
        """
        :param ignore_setting_automatic_fields:
            Skip setting fields marked as automatic, even if you provided.
            For example if the id field is marked automatic, as it's an autoincrement int.
            If `True`, setting `id=123` (commonly `id=None`) would be ignored, and instead the database assigns that value.
            If `False`, the value there will be written to the database.
            If `None`, it will be ignored as long as the value actually is None, but set if it is non-None.
            The default setting is `None`.
        :param upsert_on_conflict:
            List of fields which are expected to cause a duplicate conflict, and thus all the other fields will be overwritten.
            Either a boolean to set the automatic mode, or a list of fields.
            If `True`: Will automatically use the primary key field(s) as conflict source.
            If `False`: Will not upsert (update) but simply fail.
            If `List[str]`: Will use those given conflicting fields.
        :returns:
            The fetch parameters (`fetch_params`) for the SQL call.
            The sql string is the first tuple element, followed by the placeholder parameter values.
        """
        _ignored_fields = self.get_ignored_fields()
        _automatic_fields = self.get_automatic_fields()
        sql_fields_data = self._prepare_kwargs(**self.dict(), _allow_in=False, _fill_defaults=True)
        assert_type_or_raise(_ignored_fields, list, parameter_name='self._ignored_fields')
        assert_type_or_raise(_automatic_fields, list, parameter_name='self._automatic_fields')

        placeholder = []
        values: List[JSONType] = []
        keys = []
        upsert_fields = {}  # key is field name, values is the matching placeholder_index.
        placeholder_index = 0

        merged_fields = {}
        for sql_fields in sql_fields_data:
            keys_in_ignored_fields = [key in _ignored_fields for key in sql_fields.keys()]
            if all(keys_in_ignored_fields):
                continue
            # end if
            if any(keys_in_ignored_fields):
                raise ValueError('Some of the keys are optinal?')
            # end if
            merged_fields.update(sql_fields)
        # end for
        for key, sql_meta in merged_fields.items():
            sql_meta: SqlFieldMeta
            is_automatic_field = False
            if ignore_setting_automatic_fields or upsert_on_conflict:
                is_automatic_field = key in _automatic_fields
            if ignore_setting_automatic_fields and is_automatic_field:
                continue
            # end if
            if ignore_setting_automatic_fields is None and sql_meta.value is None:
                continue
            # end if
            placeholder_index += 1
            keys.append(f'"{key}"')
            placeholder.append(f'${placeholder_index}')
            values.append(sql_meta.value)

            if upsert_on_conflict and not is_automatic_field:
                # for upsert we can recycle the already existing values, thus the same placeholder index.
                upsert_fields[key] = placeholder_index
            # end if
        # end if

        # noinspection SqlNoDataSourceInspection,SqlResolve
        sql = f'INSERT INTO {self.get_table()} ({",".join(keys)})\n VALUES ({",".join(placeholder)})'
        if upsert_on_conflict is True:
            upsert_on_conflict_fields: List[str] = self.get_primary_keys_sql_fields()
        elif upsert_on_conflict is False:
            upsert_on_conflict_fields: List[str] = []
        else:
            upsert_on_conflict_fields: List[str] = upsert_on_conflict
        # end if
        if upsert_on_conflict_fields and upsert_fields:
            # Build additional part for the on conflict overwriting with the given fields.
            upsert_sql = ', '.join([f'"{key}" = ${placeholder_index}' for key, placeholder_index in upsert_fields.items() if key not in upsert_on_conflict_fields])
            upsert_fields_sql = ', '.join([f'"{field}"' for field in upsert_on_conflict_fields])
            sql += f'\n ON CONFLICT ({upsert_fields_sql}) DO UPDATE SET {upsert_sql}'
        # end if
        if _automatic_fields:
            # Addition so we can retrieve the updated fields (e.g. autoincrement) from the Database.
            automatic_fields_sql = ', '.join([f'"{key}"' for key in _automatic_fields])
            sql += f'\n RETURNING {automatic_fields_sql}'
        # end if
        sql += '\n;'
        # noinspection PyRedundantParentheses
        return (sql, *values)
    # end def

    @classmethod
    async def get(cls: Union[Type[CLS_TYPE], 'FastORM'], conn: Connection, **kwargs) -> Optional[CLS_TYPE]:
        """
        Retrieves a single Database element. Error if there are more matching ones.
        Like `.select(…)` but returns `None` for no matches, the match itself or an error if it's more than one row.

        :param conn:
        :param kwargs:
        :return:
        """
        rows = await cls.select(conn=conn, **kwargs)
        if len(rows) == 0:
            return None
        # end if
        assert len(rows) <= 1
        return rows[0]
    # end def

    @classmethod
    async def select(cls: Union[Type[CLS_TYPE], 'FastORM'], conn: Connection, **kwargs) -> List[CLS_TYPE]:
        """
        Get's multiple ones.
        :param conn:
        :param kwargs:
        :return:
        """
        fetch_params = cls.build_sql_select(**kwargs)
        logger.debug(f'SELECT query for {cls.__name__}: {fetch_params[0]!r} with values {fetch_params[1:]}')
        rows = await conn.fetch(*fetch_params)
        return [cls.from_row(row) for row in rows]
    # end def

    @classmethod
    def _prepare_kwargs(
        cls,
        _allow_in: bool,
        _fill_defaults: bool = False,
        **kwargs: Any
    ) -> List[Union[In[Dict[str, SqlFieldMeta[Any]]], Dict[str, SqlFieldMeta[Any]]]]:
        """
        Will parse the current classes parameters into SQL field names.
        It will handle some special cases, when you provide a FastORM element for a field as defined in the model.
        For those referencing fields you can also use the underlying primary key values directly, in case of multiple primary keys by specifying a tuple.

        Another special case handled are datetime values, those will be converted to UTC.
        For native ones, i.e. without timezone, we assume the local computer's timezone.

        Also if ` _fill_defaults` is `True`, any `None` values  will be replaced by any set default,
        as set via pydantic's `Field(default=…)` or `Field(default_factory=…)`.

        Will return a list of single elements.
        :param kwargs: Input fields, key being the python model.

        :rtype: List[Union[In[Dict[str, Any]], Dict[str, Any]]]
        :returns: List of field mappings (either a single dict or a In[…] clause with multiple dicts),
                where `{sql_key: py_value, ...}`, where there can be multiple keys in there, if we have e.g. a double primary key.

        """
        _ignored_fields = cls.get_ignored_fields()
        typehints: Dict[str, FieldInfo[Union[Type, Type[FastORM]]]] = cls.get_fields_references(recursive=True)
        typehints_pydantic: Dict[str, FieldInfo[ModelField]] = cls.get_fields_typehints(flatten_table_references=True)
        unprocessed_kwargs: Set[str] = set(kwargs.keys())
        sql_value_map: Dict[str, Union[Tuple[List[SqlFieldMeta[Any]], List[str]], Tuple[List[Any], List[str]]]] = {}  # key is the kwarg. Value is a tuple of the actual value (tuple) and the long_key(s) this needs.
        for long_key, typehint in typehints.items():
            pydantic_typehint = typehints_pydantic[long_key]
            # map it to the long database name
            short_key = typehint.unflattened_field
            if long_key in kwargs:
                kwargs_key = long_key
                unprocessed_kwargs.remove(long_key)
            elif short_key in kwargs:
                kwargs_key = short_key
                if short_key in unprocessed_kwargs:
                    # due to multiple long ones for a single short one, there might already have been deletion.
                    # as we check existence in kwargs above we're safe.
                    unprocessed_kwargs.remove(short_key)
                # end if
            else:
                # we don't have that typehint in our given keys
                continue
            # end if

            value: Any = kwargs[kwargs_key]

            # check that it's an allowed key
            if short_key in _ignored_fields:
                raise ValueError(f'key {short_key!r} is a (non-ignored) field!')
            # end if
            if not _allow_in and isinstance(value, In):
                raise TypeError('In[…] is not allowed in this type of query.')
            # end if

            if isinstance(value, datetime.datetime):
                # make sure it's UTC.
                logger.debug('datetime received: ' + repr(value))
                value = value.astimezone(tz=UTC).replace(tzinfo=None)
                logger.debug('datetime converted: ' + repr(value))
            # end if

            if _fill_defaults and value is None:
                if pydantic_typehint.referenced_type.default is not None:
                    value = pydantic_typehint.referenced_type.default
                elif pydantic_typehint.referenced_type.default_factory is not None:
                    value = pydantic_typehint.referenced_type.default_factory()
                # end if
            # end if

            if not typehint.is_reference:
                # it is not a reference
                assert short_key not in sql_value_map
                sql_value_map[short_key] = [SqlFieldMeta(value=value, sql_name=long_key, field_name=short_key, type_=typehint, field=pydantic_typehint),], [long_key,]
                continue  # easy, done
            # end if

            # now the more complex handling of references
            value = cls._resolve_referencing_kwargs(typehint, value)

            if short_key not in sql_value_map:
                sql_value_map[short_key] = [], []
            # end if
            sql_value_map[short_key][0].append(SqlFieldMeta(value=value, sql_name=long_key, field_name=short_key, type_=typehint, field=pydantic_typehint))
            sql_value_map[short_key][1].append(long_key)
        # end for
        unprocessed_kwargs: List[str] = list(unprocessed_kwargs)
        unprocessed_kwargs: List[str] = [f'{kwarg!s}={kwargs[kwarg]!r}' for kwarg in unprocessed_kwargs]
        if len(unprocessed_kwargs) == 1:
            raise ValueError(f'Unknown parameter: {unprocessed_kwargs[0]!s}')
        elif len(unprocessed_kwargs) > 1:
            raise ValueError(f'Unknown parameters: {", ".join(unprocessed_kwargs)!s}')
        # end if
        return_values: Dict[Tuple[str], In[Dict[str, Any]]] = {}
        for short_name, (sql_metas, keys) in sql_value_map.items():
            max_union = max(len(m.value) if isinstance(m.value, In) else 1 for m in sql_metas)
            for i in range(max_union):
                array_object = {}
                for key_i, long_key in enumerate(keys):
                    sql_meta = sql_metas[key_i]
                    value = sql_meta.value
                    if isinstance(value, In):
                        actual_value = typing.cast(In, value).as_list()[i]
                        array_object[long_key] = SqlFieldMeta(value=actual_value, sql_name=sql_meta.sql_name, field_name=sql_meta.field_name, type_=sql_meta.type_, field=sql_meta.field)
                    else:
                        array_object[long_key] = sql_meta
                    # end for
                # end for
                hashable_keys = tuple(keys)
                if hashable_keys in return_values:
                    if isinstance(return_values[hashable_keys], In):
                        return_values[hashable_keys].variables.append(array_object)
                    else:
                        return_values[hashable_keys] = In(return_values[hashable_keys], array_object)
                    # end if
                else:
                    return_values[hashable_keys] = array_object
                # end if
            # end for
        # end for
        # Flatten single element In's
        return [
            val.flattened_or_direct(allow_undefined=False) if isinstance(val, In) else val
            for val in return_values.values()
        ]
    # end def

    @classmethod
    def _prepare_kwargs_flattened(cls, **kwargs: Any) -> List[SqlFieldMeta[Any]]:
        kwargs = cls._prepare_kwargs(_allow_in=False, **kwargs)
        new_list = []
        for dictionary in kwargs:
            new_list.extend(dictionary.values())
        # end for
        return new_list
    # end def


    @classmethod
    def _resolve_referencing_kwargs(cls, typehint, value) -> Union[Any, In]:
        if isinstance(value, In):
            # so you used Union[variable_a, variable_b]
            # we wanna resolve both variables.
            return In(*(cls._resolve_referencing_kwargs(typehint, variable) for variable in value))
        # end if
        for i, type_info in enumerate(typehint.types[1:]):
            if isinstance(value, tuple):
                value: Tuple[Any, ...]
                # get it by keywords position
                #
                # because we start counting at `i = 0` but list index starts with the second item, `[:1]`,
                # this `i` is effectively `index - 1`.
                last_type = typehint.types[i].type_
                current_field = type_info.field
                primary_keys: List[str] = typing.cast(Type[FastORM], last_type).get_primary_keys_keys()
                primary_key_position = primary_keys.index(current_field)
                value: Any = value[primary_key_position]  # this should be the tuple position, because the tuple should be the primary keys.
                continue
            # end if

            if isinstance(value, FastORM):
                value: FastORM
                # get it by primary key fields
                value: Any = getattr(value, type_info.field)  # easy actually. Just grab it.
                continue
            # end if

            # So now we know it must be a native value, e.g. the primary key's actual value.
            value: Any
            break  # so no further processing needs to be done.
        # end for
        return value
    # end def

    @classmethod
    def _prepared_dict_to_sql(cls, sql_variable_dict: Dict[str, Any], placeholder_index: int):
        """
        Builds a query/insert sql string, based on the sql_variable dict (result of _prepare_kwargs())


        **`sql_variable_dict`** can contain one or more items:

        For one item it's gonna result in something like
         - `"foo" = $1`
        If it has more than one field (dict entry) it will do a combined key for those:
         - `("foo1", "foo2") = ($1, $2)`

        :param sql_variable_dict:
        :param placeholder_index:
        :return: key_string, placeholder_string, values_list, placeholder_index_after
        """
        assert isinstance(sql_variable_dict, dict)  # Not In!
        if len(sql_variable_dict) == 1:
            placeholder_index_after = placeholder_index + 1
            long_key, value = list(sql_variable_dict.items())[0]
            key_string = f'"{long_key}"'
            placeholder_string = f'${placeholder_index_after}'
            values_list = [value]
        else:  # is_in_list_clause is True
            key_string = ", ".join(f'"{long_key}"' for long_key in sql_variable_dict.keys()).join("()")
            placeholder_index_after = placeholder_index + len(sql_variable_dict)
            placeholder_string = ", ".join(f'${i}' for i in range(placeholder_index + 1, placeholder_index_after + 1)).join("()")
            values_list = list(sql_variable_dict.values())
        # end if
        return key_string, placeholder_string, values_list, placeholder_index_after
    # end def

    @classmethod
    def build_sql_select(cls, **kwargs):
        """
        Builds a `SELECT` query.

        It will handle some special cases, when you provide a FastORM element for a field as defined in the model. For those referencing fields you can also use the underlying primary key values directly, in case of multiple primary keys by specifying a tuple.
        Also you can specify a list of multiple values to have it generate a `field IN (…)` clause.
        :param kwargs:
        :return:
        """
        typehints: Dict[str, FieldInfo[Type]] = cls.get_fields_references(recursive=True)
        non_ignored_long_names = [long_name for long_name, typehint in typehints.items() if typehint.unflattened_field not in cls.get_ignored_fields()]
        fields = ','.join([
            f'"{field}"'
            for field in non_ignored_long_names
            if not field.startswith('_')
        ])
        sql_where = cls._prepare_kwargs(**kwargs, _allow_in=True)
        where_index = 0
        where_parts = []
        where_values = []
        # noinspection PyUnusedLocal
        where_wolf = None

        for sql_wheres in sql_where:
            sql_wheres: Union[In[Dict[str, Any]], Dict[str, Any]]

            if not sql_wheres:  # it's empty
                continue
            # end if

            if not isinstance(sql_wheres, In):
                if not all(meta.value is None for meta in sql_wheres.values()):
                    key_string, placeholder_string, values_list, where_index = cls._prepared_dict_to_sql(sql_variable_dict=sql_wheres, placeholder_index=where_index)
                    where_values.extend(values_list)
                    where_parts.append(f'{key_string} = {placeholder_string}')
                else:  # it is None
                    # basically we need no placeholder stuff, so we can save a lot
                    key_string, _, _, _ = cls._prepared_dict_to_sql(sql_variable_dict=sql_wheres, placeholder_index=0)
                    where_parts.append(f'{key_string} IS NULL')
                # end if
            else:
                key_string = None
                placeholder_strings = []
                can_be_null = False
                for actual_wheres in sql_wheres.variables:
                    if all(value is None for value in actual_wheres.values()):
                        can_be_null = True
                    else:  # is not None
                        key_string_new, placeholder_string, values_list, where_index = cls._prepared_dict_to_sql(sql_variable_dict=actual_wheres, placeholder_index=where_index)
                        where_values.extend(values_list)
                        placeholder_strings.append(placeholder_string)
                        assert key_string is None or key_string_new == key_string  # make sure once more it's consistently the same
                        key_string = key_string_new
                    # end if
                # end for
                is_null_part = f"OR {key_string} IS NULL" if can_be_null else ""
                where_parts.append(f'{key_string} IN ({", ".join(placeholder_strings)}) {is_null_part}'.strip())
            # end if
        # end if
        where_sql = "" if not where_parts else f' WHERE {" AND ".join(where_parts)}'
        where_values = [where_value.value for where_value in where_values]

        # noinspection SqlResolve,SqlNoDataSourceInspection
        sql = f'SELECT {fields} FROM "{cls._table_name}"{where_sql}'
        # noinspection PyRedundantParentheses
        return (sql, *where_values)
    # end def

    def _insert_preparation(
        self,
        ignore_setting_automatic_fields: Optional[bool] = None,
        upsert_on_conflict: Union[List[str], bool] = False,
        on_conflict_upsert_field_list: None = None,  # deprecated! Use `upsert_on_conflict=…` instead!
    ) -> Tuple[Any, ...]:
        """
        Preparation step for `.insert(…)`. See that method for more information.

        :param ignore_setting_automatic_fields:
            Skip setting fields marked as automatic, even if you provided.
            For example if the id field is marked automatic, as it's an autoincrement int.
            If `True`, setting `id=123` (commonly `id=None`) would be ignored, and instead the database assigns that value.
            If `False`, the value there will be written to the database.
            If `None`, it will be ignored as long as the value actually is None, but set if it is non-None.
            The default setting is `None`.
        :param upsert_on_conflict:
            List of fields which are expected to cause a duplicate conflict, and thus all the other fields will be overwritten.
            Either a boolean to set the automatic mode, or a list of fields.
            If `True`: Will automatically use the primary key field(s) as conflict source.
            If `False`: Will not upsert (update) but simply fail.
            If `List[str]`: Will use those given conflicting fields.
        :param on_conflict_upsert_field_list: Deprecated and will be removed in the future. Use `upsert_on_conflict` instead.
        :returns:
            The fetch parameters (`fetch_params`) for the SQL call.
            The sql string is the first tuple element, followed by the placeholder parameter values.

        :used-by: insert
        """
        if on_conflict_upsert_field_list is not None:
            logger.warn(
                "FastORM.insert(…)'s `on_conflict_upsert_field_list=…` is deprecated, use `upsert_on_conflict=…` instead."
            )
            if upsert_on_conflict:
                raise ValueError(
                    "Only use the new `upsert_on_conflict`. Don't use the old `on_conflict_upsert_field_list`."
                )
            # end if
            assert isinstance(on_conflict_upsert_field_list, list)  # we don't bother with proper type errors as you should use `upsert_on_conflict` instead anyway.
            upsert_on_conflict = on_conflict_upsert_field_list
        # end if
        assert_type_or_raise(upsert_on_conflict, list, bool, parameter_name="upsert_on_conflict")
        fetch_params: Tuple[Any, ...] = self.build_sql_insert(
            ignore_setting_automatic_fields=ignore_setting_automatic_fields,
            upsert_on_conflict=upsert_on_conflict,
        )
        self._database_cache_overwrite_with_current()
        if VERBOSE_SQL_LOG:
            fetch_params_debug = "\n".join([f"${i}={param!r}" for i, param in enumerate(fetch_params)][1:])
            logger.debug(f'INSERT query for {self.__class__.__name__}\nQuery:\n{fetch_params[0]}\nParams:\n{fetch_params_debug!s}')
        else:
            logger.debug(f'INSERT query for {self.__class__.__name__}: {fetch_params!r}')
        # end if
        return fetch_params
    # end def

    def _insert_postprocess(
        self,
        updated_automatic_values_rows: List[Union[Record, object]],
        ignore_setting_automatic_fields: Optional[bool] = None,
        write_back_automatic_fields: bool = True,
    ) -> None:
        """
        Writes back the updated data from the database to the object, especially the `_automatic_fields`.

        :param updated_automatic_values_rows:
            The result of the executed database query. List of objects containing the rows.
        :param ignore_setting_automatic_fields:
            Skip setting fields marked as automatic, even if you provided.
            For example if the id field is marked automatic, as it's an autoincrement int.
            If `True`, setting `id=123` (commonly `id=None`) would be ignored, and instead the database assigns that value.
            If `False`, the value there will be written to the database.
            If `None`, it will be ignored as long as the value actually is None, but set if it is non-None.
            The default setting is `None`.
        :param write_back_automatic_fields: Apply the automatic fields back to this object.
                                            Ignored if `ignore_setting_automatic_fields` is False.
        :return: None
        :used-by: insert
        """
        _automatic_fields = self.get_automatic_fields()
        if _automatic_fields:
            assert len(updated_automatic_values_rows) == 1
            updated_automatic_values = updated_automatic_values_rows[0]
            if ignore_setting_automatic_fields is not False and write_back_automatic_fields:
                for field in _automatic_fields:
                    assert field in updated_automatic_values
                    setattr(self, field, updated_automatic_values[field])
                    self._database_cache[field] = updated_automatic_values[field]
                # end for
            # end if
        else:
            assert len(updated_automatic_values_rows) == 0
        # end if
    # end def

    async def insert(
        self: Union[CLS_TYPE, "_BaseFastORM"], conn: Connection, *,
        ignore_setting_automatic_fields: Optional[bool] = None,
        upsert_on_conflict: Union[List[str], bool] = False,
        write_back_automatic_fields: bool = True,
        on_conflict_upsert_field_list: None = None,  # deprecated! Use `upsert_on_conflict=…` instead!
    ) -> Union[CLS_TYPE, "_BaseFastORM"]:
        """
        :param conn: Database connection to run at.
        :param ignore_setting_automatic_fields:
            Skip setting fields marked as automatic, even if you provided.
            For example if the id field is marked automatic, as it's an autoincrement int.
            If `True`, setting the field to a value in your data, for example. `id=123` (but commonly `id=None`) would
                       be ignored, and instead the database has to assign that value.
            If `False`, the value in that field will be written to the database, even if it is `None`.
            If `None`, it will be ignored as long as the value actually is `None`, and the database has to setit,
                       but set if it is non-`None`.
            The default setting is `None`.
        :param upsert_on_conflict:
            List of fields which are expected to cause a duplicate conflict, and thus all the other fields will be overwritten.
            Either a boolean to set the automatic mode, or a list of fields.
            If `True`: Will automatically use the primary key field(s) as conflict source.
            If `False`: Will not upsert (update) but simply fail.
            If `List[str]`: Will use those given conflicting fields.
        :param on_conflict_upsert_field_list: Deprecated and will be removed in the future. Use `upsert_on_conflict` instead.
        :param write_back_automatic_fields: Apply the automatic fields back to this object.
                                            Ignored if `ignore_setting_automatic_fields` is False.
        :return: self
        """
        fetch_params = self._insert_preparation(
            ignore_setting_automatic_fields=ignore_setting_automatic_fields,
            upsert_on_conflict=upsert_on_conflict,
            on_conflict_upsert_field_list=on_conflict_upsert_field_list,
        )
        updated_automatic_values_rows: List[Record] = await conn.fetch(*fetch_params)
        logger.debug(f'INSERT for {self.__class__.__name__}: {updated_automatic_values_rows} for {self}')
        self._insert_postprocess(
            updated_automatic_values_rows=updated_automatic_values_rows,
            ignore_setting_automatic_fields=ignore_setting_automatic_fields,
            write_back_automatic_fields=write_back_automatic_fields,
        )
        return self
    # end def

    def build_sql_update(self):
        """
        Builds a prepared SQL statement for update.
        Only fields with changed values will be updated in the database.
        However this one doesn't resets the cache for those, see `FastORM.update(…)` for that.

        :return: The SQL string followed by positional parameters for the `conn.execute(…)` method.
        """
        _table_name = getattr(self, '_table_name')
        _primary_keys = getattr(self, '_primary_keys')
        _database_cache = getattr(self, '_database_cache')
        _automatic_fields = self.get_automatic_fields()
        assert_type_or_raise(_table_name, str, parameter_name='self._table_name')
        assert_type_or_raise(_primary_keys, list, parameter_name='self._primary_keys')
        assert_type_or_raise(_database_cache, dict, parameter_name='self._database_cache')
        assert_type_or_raise(_automatic_fields, list, parameter_name='self._automatic_fields')

        # SET ...
        update_values = self.get_changes()
        prepared_sql_fields = self._prepare_kwargs(**update_values, _allow_in=False)

        # UPDATE ... SET ... WHERE ...
        placeholder_index = 0
        values: List[Any] = []
        update_keys: List[str] = []  # "foo" = $1
        for sql_fields in prepared_sql_fields:
            sql_fields: Dict[str, Any]
            key_string, placeholder_string, values_list, placeholder_index = self._prepared_dict_to_sql(sql_variable_dict=sql_fields, placeholder_index=placeholder_index)
            values.extend(values_list)
            update_keys.append(f'{key_string} = {placeholder_string}')
        # end if

        # WHERE pk...
        primary_key_where: List[str] = []  # "foo" = $1
        for primary_key in _primary_keys:
            if primary_key in _database_cache:
                value = _database_cache[primary_key]
            else:
                value = getattr(self, primary_key)
            # end if
            placeholder_index += 1
            primary_key_where.append(f'"{primary_key}" = ${placeholder_index}')
            values.append(value)
        # end if
        logger.debug(f'Fields to UPDATE for selector {primary_key_where!r}: {update_values!r}')

        assert update_keys
        sql = f'UPDATE "{_table_name}"\n'
        sql += f' SET {",".join(update_keys)}\n'
        sql += f' WHERE {" AND ".join(primary_key_where)}\n'
        sql += ';'
        # while primary keys will be native values, the current class' values will be SqlFieldMeta, which have to be unpacked.
        values = [value.value if isinstance(value, SqlFieldMeta) else value for value in values]

        # noinspection PyRedundantParentheses
        return (sql, *values)
    # end def

    def get_changes(self) -> Dict:
        """
        Returns all values which got changed and are now different to the last downloaded database version.
        """
        own_keys = self.get_fields()
        _database_cache = self._database_cache
        _ignored_fields = self.get_ignored_fields()
        assert_type_or_raise(own_keys, list, parameter_name='own_keys')
        assert_type_or_raise(_database_cache, dict, parameter_name='self._database_cache')
        assert_type_or_raise(_ignored_fields, list, parameter_name='self._ignored_fields')

        update_values: Dict[str, Any] = {}
        for key in own_keys:
            if key.startswith('_') or key in _ignored_fields:
                continue
            # end if
            value = getattr(self, key)
            if key not in _database_cache:
                update_values[key] = value
            elif _database_cache[key] != value:
                update_values[key] = value
            # end if
        # end if
        return update_values
    # end def

    def has_changes(self) -> bool:
        """
        :returns: if we have unsaved changes.
        """
        return bool(self.get_changes())
    # end if

    async def update(self, conn: Connection) -> None:
        """
        Update the made changes to the database.
        Only fields with changed values will be updated in the database.

        :uses: FastORM.build_sql_update()
        """
        if not getattr(self, '_database_cache', None):
            return  # nothing to do.
        # end if
        fetch_params = self.build_sql_update()
        logger.debug(f'UPDATE query for {self.__class__.__name__}: {fetch_params!r}')
        update_status = await conn.execute(*fetch_params)
        logger.debug(f'UPDATE for {self.__class__.__name__}: {update_status} for {self}')
        self._database_cache_overwrite_with_current()
    # end if

    def build_sql_delete(self):
        _database_cache = self._database_cache
        assert_type_or_raise(_database_cache, dict, parameter_name='self._database_cache')

        typehints = self.get_fields_typehints(flatten_table_references=True)

        # DELETE FROM "name" WHERE pk...
        where_values = []
        placeholder_index = 0
        primary_key_parts: List[str] = []  # "foo" = $1
        kwargs = {}

        # collect data
        for primary_key_long, field_info in typehints.items():
            if not field_info.is_primary_key:
                continue
            # end if
            primary_key_short = field_info.unflattened_field
            if primary_key_long in _database_cache:
                kwargs[primary_key_long] = _database_cache[primary_key_long]
            elif primary_key_short in _database_cache:
                kwargs[primary_key_short] = _database_cache[primary_key_short]
            else:
                kwargs[primary_key_short] = getattr(self, primary_key_short)
            # end if
        # end for

        # make sure we do allow all reference formats in the data. (e.g. Tuples and TableObjects)
        new_fields = self._prepare_kwargs_flattened(**kwargs)

        # built where tuples for which rows to delete
        for sql_field_meta in new_fields:
            placeholder_index += 1
            primary_key_parts.append(f'"{sql_field_meta.sql_name}" = ${placeholder_index}')
            where_values.append(sql_field_meta.value)
        # end if
        logger.debug(f'Fields to DELETE for selector {primary_key_parts!r}: {where_values!r}')

        # noinspection SqlWithoutWhere,SqlResolve,SqlNoDataSourceInspection
        sql = f'DELETE FROM {self.get_table()}\n'
        sql += f' WHERE {" AND ".join(primary_key_parts)}'
        sql += '\n;'
        # noinspection PyRedundantParentheses
        return (sql, *where_values)
    # end def

    async def delete(self, conn: Connection):
        fetch_params = self.build_sql_delete()
        logger.debug(f'DELETE query for {self.__class__.__name__}: {fetch_params!r}')
        delete_status = await conn.execute(*fetch_params)
        logger.debug(f'DELETE for {self.__class__.__name__}: {delete_status} for {self}')
        self._database_cache_remove()
    # end if

    def clone(self: CLS_TYPE) -> CLS_TYPE:
        return self.__class__(**self.as_dict())
    # end if

    @classmethod
    def get_primary_keys_keys(cls) -> List[str]:
        return cls._primary_keys if hasattr(cls, '_primary_keys') else []
    # end def

    @classmethod
    def get_primary_keys_sql_fields(cls) -> List[str]:
        """
        Returns the actual sql fields of the primary keys.
        They are not quoted or escaped in any way.
        :return:
        """
        keys = cls.get_primary_keys_keys()
        hints = cls.get_fields_references(recursive=True)
        # 'reference_to_other_table__id_part_1': FieldInfo(is_primary_key=False, types=[FieldItem(field='reference_to_other_table', type_=<class 'fastorm.OtherTable'>), FieldItem(field='id_part_1', type_=<class 'int'>)])
        sql_fields: List[str] = []
        for sql_field, hint in hints.items():
            if hint.unflattened_field in keys:
                sql_fields.append(sql_field)
            # end if
        # end for
        return sql_fields
    # end def

    def get_primary_keys(self) -> Dict[str, Any]:
        _primary_keys = self.get_primary_keys_keys()
        return {k: v for k, v in self.as_dict().items() if k in _primary_keys}
    # end def

    def get_primary_keys_values(self):
        return list(self.get_primary_keys().values())
    # end def

    @classmethod
    def get_primary_keys_typehints(cls) -> Dict[str, FieldInfo[ModelField]]:
        _primary_keys = cls.get_primary_keys_keys()
        type_hints = cls.get_fields_typehints(flatten_table_references=False)
        return {key: hint for key, hint in type_hints.items() if key in _primary_keys}
    # end def

    @classmethod
    def get_primary_keys_type_annotations(cls, ref_as_union_with_pk: bool = False) -> Dict[str, FieldInfo[ModelField]]:
        _primary_keys = cls.get_primary_keys_keys()
        _annotations = getattr(cls, '__annotations__' if ref_as_union_with_pk else '__original__annotations__', {})
        return {key: hint for key, hint in _annotations.items() if key in _primary_keys}
    # end def

    @classmethod
    def from_row(
        cls: Union[Type[CLS_TYPE], 'FastORM'],
        row: Union[Dict[str, Any], Union[List[Any], Tuple[Any]]],
    ) -> CLS_TYPE:
        """
        Load a query result row into this class type.
        It's done automatically for you if you use `.get(…)` or `.select(…)`.
        However, for advanced raw SQL queries this can be helpful,
        especially when combined with `get_select_fields(…)` to make sure you're not missing a field.
        :param row: Either a dict, which can contain a space separated namespace prefix
                    in the key (see `.get_select_fields(namespace="…")`, e.g. `"the_namespace field1"`).
                    Or a list/tuple of values matching the order of `.get_select_fields(…)`.
        :return: An object containing the data.
        """
        assert_type_or_raise(row, (dict, list, tuple, asyncpg.Record), parameter_name='row')
        if isinstance(row, (list, tuple)):
            fields = cls.get_sql_fields()
            row = {fields[i]: row[i] for i in range(min(len(row), len(fields)))}
        # end if
        row_data = {key.rsplit(" ")[-1]: value for key, value in dict(row).items()}  # handles the namespaces like "namespace_name field_name"
        processed = cls._prepare_kwargs_flattened(**row_data)
        kwargs = {sql_meta.field_name: sql_meta.value for sql_meta in processed}
        # noinspection PyArgumentList
        instance = cls(**kwargs)
        instance._database_cache_overwrite_with_current()
        return instance
    # end def

    @classmethod
    def consume_from_row(cls: Union[Type[CLS_TYPE], 'FastORM'], row: list) -> Tuple[CLS_TYPE, list]:
        """
        Like `.from_row(row)`, but will consume many columns as the `.get_sql_fields()` returns fields from the given row.
        From those an instance of this class will be constructed and returned as the first tuple element.
        The reminder list of columns will be returned as the second tuple element.

        :param row: List of column data.
                    Must be the at least the same length and be the same order as `.get_sql_fields()` returns fields.

        :return: a new instance of this class and the leftover fields.
        """
        own_fields_count = cls.get_select_fields_len()
        row_data, row_reminder = row[:own_fields_count], row[own_fields_count:]
        instance = cls.from_row(row_data)
        return instance, row_reminder
    # end def

    _COLUMN_AUTO_TYPES: Dict[type, str] = {
        int: "BIGSERIAL",
    }

    _COLUMN_TYPES: Dict[type, str] = {
        bool: "BOOLEAN",
        bytes: "BYTEA",
        bytearray: "BYTEA",
        str: "TEXT",
        # Python Type
        # PostgreSQL Type
        # Source: https://magicstack.github.io/asyncpg/current/usage.html#type-conversion

        # anyenum
        # str

        # anyrange
        # asyncpg.Range

        # record
        # asyncpg.Record, tuple, Mapping

        # bit, varbit
        # asyncpg.BitString

        asyncpg.Box: "BOX",

        # cidr
        # ipaddress.IPv4Network, ipaddress.IPv6Network
        ipaddress.IPv4Network: "CIDR",
        ipaddress.IPv6Network: "CIDR",

        # inet
        # ipaddress.IPv4Interface, ipaddress.IPv6Interface, ipaddress.IPv4Address, ipaddress.IPv6Address
        ipaddress.IPv4Interface: "INET",
        ipaddress.IPv6Interface: "INET",
        ipaddress.IPv4Address: "INET",
        ipaddress.IPv6Address: "INET",

        # macaddr
        # str

        # time
        # offset-naïve datetime.time

        # time with time zone
        # offset-aware datetime.time
        datetime.time: "TIME",

        # timestamp
        # offset-naïve datetime.datetime
        #  +
        # timestamp with time zone
        # offset-aware datetime.datetime
        datetime.datetime: "TIMESTAMP",

        datetime.date: "DATE",  # must come after datetime.datetime as datetime is a subclass of this

        datetime.timedelta: "INTERVAL",

        # float, double precision
        # float [2]
        # Inexact single-precision float values may have a different representation when decoded into a Python float. This is inherent to the implementation of limited-precision floating point types. If you need the decimal representation to match, cast the expression to double or numeric in your query.
        float: "DOUBLE PRECISION",

        # smallint, integer, bigint
        # int
        int: "BIGINT",

        # numeric
        # Decimal
        decimal.Decimal: "NUMERIC",

        # json, jsonb
        # str
        dict: "JSONB",
        list: "JSONB",  # the special cases like INT[] will be processed beforehand.

        # line
        # asyncpg.Line
        asyncpg.Line: "LINE",

        # lseg
        # asyncpg.LineSegment
        asyncpg.LineSegment: "LSEG",

        # money
        # str

        asyncpg.Circle: "CIRCLE",

        # point
        # asyncpg.Point
        asyncpg.Point: "POINT",

        # polygon
        # asyncpg.Polygon
        asyncpg.Polygon: "POLYGON",

        # path
        # asyncpg.Path
        asyncpg.Path: "PATH",

        # uuid
        # uuid.UUID
        uuid.UUID: "UUID",

        BaseModel: "JSONB",
    }

    _COLUMN_TYPES_SPECIAL: Dict[Callable[[type], bool], str] = {
        lambda cls: hasattr(cls, 'to_dict'): _COLUMN_TYPES[dict],
        lambda cls: hasattr(cls, 'to_array'): _COLUMN_TYPES[dict],  # pytgbot object uses to_array
    }

    _COLUMN_AUTO_TYPES_SPECIAL: Dict[Callable[[type], bool], str] = {
    }

    @classmethod
    def _match_type(cls, python_type: type, *, automatic: bool) -> str:
        try:
            issubclass(python_type, object)
        except TypeError:  # issubclass() arg 1 must be a class
            raise TypeError(f'Could not process type {python_type} as a python type. Probably a typing annotation?.')
        if automatic:
            for sql_py_type, sql_type in cls._COLUMN_AUTO_TYPES.items():
                if issubclass(python_type, sql_py_type):
                    return sql_type
                # end if
            # end for
            for check_function, sql_type in cls._COLUMN_AUTO_TYPES_SPECIAL.items():
                if check_function(python_type):
                    return sql_type
                # end if
            # end for
        # end if
        for sql_py_type, sql_type in cls._COLUMN_TYPES.items():
            if issubclass(python_type, sql_py_type):
                return sql_type
            # end if
        # end for
        for check_function, sql_type in cls._COLUMN_TYPES_SPECIAL.items():
            if check_function(python_type):
                return sql_type
            # end if
        # end for
        raise TypeError(f'Could not process type {python_type} as database type.')
    # end def

    @classmethod
    async def create_table(
        cls,
        conn: Connection,
        if_not_exists: bool = False,
        psycopg2_conn: Union['psycopg2.extensions.connection', 'psycopg2.extensions.cursor', None] = None,
    ):
        """
        Builds and executes a CREATE TABLE statement.

        :param conn: the `asyncpg` database connection to execute this with.

        :param if_not_exists:
            If the table definition should include IF NOT EXISTS, thus not producing an error if it does, but instead being silently ignored.

        :param psycopg2_conn:
            If you have complex default types for your fields (everything other than None, bool, int, and pure ascii strings),
            The psycopg2 library is used to build a injection safe SQL string.
            Therefore then psycopg2 has to be installed (pip install psycopg2-binary),
            and a connection (or cursor) to the database must be provided (psycopg2_conn = psycopg2.connect(…)).
        :return:
        """
        create_params = cls.build_sql_create(if_not_exists=if_not_exists, psycopg2_conn=psycopg2_conn if psycopg2_conn else conn)
        logger.debug(f'CREATE query for {cls.__name__}: {create_params!r}')
        create_status = await conn.execute(*create_params)
        logger.debug(f'CREATEed {cls.__name__}: {create_status}')
    # end if

    @classmethod
    def build_sql_create(
        cls,
        if_not_exists: bool = False,
        psycopg2_conn: Union['psycopg2.extensions.connection', 'psycopg2.extensions.cursor', None] = None,
    ) -> Tuple[str, Any]:
        """
        Builds a CREATE TABLE statement.

        :param if_not_exists:
            If the table definition should include IF NOT EXISTS, thus not producing an error if it does, but instead being silently ignored.

        :param psycopg2_conn:
            If you have complex default types for your fields (everything other than None, bool, int, and pure ascii strings),
            The psycopg2 library is used to build a injection safe SQL string.
            Therefore then psycopg2 has to be installed (pip install psycopg2-binary),
            and a connection to the database must be provided.
            This can be either a psycopg2 connection (psycopg2_conn = psycopg2.connect(…)),
            a psycopg2 cursor.
            If a asyncpg Connection is given we try to create a psycopg2 connection from it automatically.
        :return:
        """
        assert issubclass(cls, BaseModel)  # because we no longer use typing.get_type_hints, but pydantic's `cls.__fields__`
        _table_name = cls.get_name()
        _automatic_fields = cls.get_automatic_fields()
        assert_type_or_raise(_table_name, str, parameter_name='cls._table_name')
        _ignored_fields = cls.get_ignored_fields()

        type_hints = cls.get_fields_typehints(flatten_table_references=True)
        primary_keys = [key for key, field_typehint in type_hints.items() if field_typehint.is_primary_key]
        single_primary_key = len(primary_keys) == 1

        # .required tells us if we have a default value set or not.
        # .allow_none tells us if None is supported
        # .default tells us what default (or None)

        placeholder_index = 0
        placeholder_values = []
        type_definitions = []
        all_defaults_are_simple_types_and_save_to_concatenate = True  # they only contain ints, boolean and None

        for key, type_hint_info in type_hints.items():
            type_hint = type_hint_info.resulting_type
            is_automatic_field = key in _automatic_fields

            # start processing the default_factory of the current field (not the resolved one),
            # check if it's an AutoincrementType type.
            if isinstance(type_hint_info.referenced_type.default_factory, AutoincrementType):
                is_automatic_field = True
            # end if

            is_optional, sql_type = cls.match_type(type_hint=type_hint, is_automatic_field=is_automatic_field, key=key)
            if type_hint.allow_none:
                is_optional = True
            # end if

            if (
                any(
                    not sub_hint.type_.required and
                    # using sub_hint.field_info.* instead of sub_hint.* as there a non set default will actually be `pydantic.Undefined` and thus can't be confused with None.
                    # now either the default is already None. Alternatively it doesn't has a default and .allow_none is set to true.
                    (sub_hint.type_.field_info.default is None or (isinstance(sub_hint.type_.field_info.default, UndefinedType) and sub_hint.type_.allow_none)) and
                    # also there shouldn't be a default factory. But we allow some special cases which have no actual default "value" meaning and can be treated as no default factory.
                    (sub_hint.type_.field_info.default_factory is None or isinstance(sub_hint.type_.field_info.default_factory, (AutoincrementType,)))

                    for sub_hint in type_hint_info.types
                )
            ):
                is_optional = True
            # end if

            if is_automatic_field:
                # if it is an automatic field, the optional must have been so it can be left empty when creating a new object.
                is_optional = False
            # end if

            # Now let's build that column's sql part

            # column_name, data_type:
            type_definition_parts = [f'  "{key}"', sql_type]

            # column_constraints:
            if not is_optional:
                type_definition_parts.append("NOT NULL")
            # end if

            # has it a default value?
            if not isinstance(type_hint.field_info.default, UndefinedType):
                type_definition_parts.append(f'DEFAULT {{default_placeholder_{placeholder_index}}}')
                placeholder_index += 1
                default_value = type_hint.field_info.default
                if not (
                    default_value is None or
                    isinstance(default_value, (int, bool)) or
                    (isinstance(default_value, str) and default_value.isascii())  # py 3.7+, see https://stackoverflow.com/a/51141941/3423324#how-to-check-if-a-string-in-python-is-in-ascii
                ):
                    all_defaults_are_simple_types_and_save_to_concatenate = False
                # end if
                placeholder_values.append(default_value)
            # end if
            if single_primary_key and key in primary_keys:
                type_definition_parts.append('PRIMARY KEY')
            # end if

            # merge the line
            type_definitions.append(" ".join(type_definition_parts))
        # end for
        sql_lines = [
            f"CREATE TABLE {'IF NOT EXISTS ' if if_not_exists else ''}{cls.get_table()} (",
            ",\n".join(type_definitions),
        ]
        if len(primary_keys) > 1:
            sql_lines[-1] += ','
            # keys = [key for key, item in type_hints.items()]
            sql_lines.append(f'''  PRIMARY KEY ({', '.join(f'"{key}"' for key in primary_keys)})''')
        # end if
        sql_lines.append(");")

        sql = "\n".join(
            sql_lines
        )
        if placeholder_values:
            # we do have at least one row like
            # '"blah" SOME_TYPE ... DEFAULT {default_placeholder_0}'
            # or maybe even more of those.
            # Now we need to fill them in.
            # To do it safely, we use `psycopg2.sql.SQL` and `psycopg2.sql.Literal` as soon as we have complex types.
            # Complex means anything which isn't None (NULL), bool (true/false), ints (0,1,42,69,4458, etc.) and pure ascii strings (not “ª∂ﬁ˜ªæäß»Íƒ†”, etc.)
            # We also do that if we detect psycopg2 and a given psycopg2_conn connection, so a user can be on the save side with having that installed and providing a connection.
            # In fact, `FastORM.create_table(…)` already shares the connection details with us, in that case we automatically use the save approach when you got psycopg2 installed.
            if all_defaults_are_simple_types_and_save_to_concatenate and not (psycopg2_conn is not None and psycopg2):
                # special case where it's enough to use python only formatting, as we have only NONE, bool, ints and pure ascii strings.
                formatting_dict = {}
                for i, default_value in enumerate(placeholder_values):
                    assert_type_or_raise(default_value, None, bool, int, str, parameter_name=f'default_values[{i}]')
                    if default_value is None:
                        sql_value = 'NULL'
                    elif isinstance(default_value, bool):
                        sql_value = 'true' if default_value else 'false'
                    elif isinstance(default_value, int):
                        sql_value = str(default_value)
                    else:  # str
                        assert isinstance(default_value, str)
                        assert default_value.isascii()
                        sql_value = "'" + default_value.replace("'", "''") + "'"  # this SHOULD be enough... hopefully.  If you don't trust it either, install psycopg2 and provide a connection.
                    # end if
                    formatting_dict[f'default_placeholder_{i}'] = sql_value
                # end if
                sql = sql.format(**formatting_dict)
            else:
                psycopg2_conn = cls._as_psycopg2_connection(psycopg2_conn)
                placeholder_values = [psycopg2.sql.Literal(value) for value in placeholder_values]
                formatting_dict = {f'default_placeholder_{i}': val for i, val in enumerate(placeholder_values)}
                sql = psycopg2.sql.SQL(sql)
                sql = sql.format(**formatting_dict).as_string(context=psycopg2_conn)
            # end if
        # noinspection PyRedundantParentheses
        return (sql, *[])
    # end def

    @classmethod
    def _as_psycopg2_connection(cls, conn):
        if psycopg2 is None:
            # so we had an import error earlier (on top of the file)
            # raise a proper ImportError message with useful information
            raise ImportError(
                'For using complex default values (everything other than None, bool, int, and pure ascii strings) '
                'psycopg2 needs to be installed (pip install psycopg2-binary).'
            )
        # end if
        try:
            assert_type_or_raise(conn, Connection, psycopg2.extensions.connection, psycopg2.extensions.cursor, parameter_name='psycopg2_conn')
        except TypeError:
            # enhance error message with useful information
            error_class = ValueError if conn is None else TypeError
            raise error_class(
                'For using complex default values (everything other than None, bool, int, and pure ascii strings) '
                'a psycopg2 connection or cursor needs to be provided as the psycopg2_conn parameter.'
            )
        # end try
        if isinstance(conn, Connection):
            # if it's a asyncpg Connection, try to build the needed psycopg2 connection from it.
            # noinspection PyProtectedMember
            params = dict(
                database=conn._params.database,  # aka dbname
                user=conn._params.user,
                password=conn._params.password,
                host=conn._addr[0] if isinstance(conn._addr, tuple) else conn._addr,
                port=conn._addr[1] if isinstance(conn._addr, tuple) else None,
            )
            # https://regex101.com/r/bNWUzG/1/
            unix_socket_match = re.match(r'^(?P<path>/.*)(?<=/)\.s\.PGSQL\.(?P<port>\d+)$', params['host'])
            if unix_socket_match:
                params['host'] = unix_socket_match.group('path')
                params['port'] = int(unix_socket_match.group('port'))
            # end if
            conn = psycopg2.connect(**params)
        # end if
        return conn
    # end def

    @classmethod
    async def create_table_references(
        cls,
        conn: Connection,
    ):
        """
        Builds and executes a ALTER TABLE and CREATE TABLE statement.

        :param conn: the `asyncpg` database connection to execute this with.
        :return:
        """
        reference_params = cls.build_sql_references()
        logger.debug(f'REFERENCE query for {cls.__name__}: {reference_params!r}')
        if reference_params[0] == SQL_DO_NOTHING:
            logger.debug(f'REFERENCEed {cls.__name__}: No need to do anything.')
            return
        # end if
        reference_status = await conn.execute(*reference_params)
        logger.debug(f'REFERENCEed {cls.__name__}: {reference_status}')
    # end if

    @classmethod
    def build_sql_references(cls) -> Tuple[str, ...]:
        """
        Prepare the query for generating references between tables, including the indexes on the outgoing fields.
        In case we don't have any references to other tables, `fastorm.SQL_DO_NOTHING` will be returned as SQL parameter.
        This is also a valid SQL string which does nothing in your database, but can be executed.
        This way there won't be an error for users blindly executing the stuff returned from this function.
        But if you wanna save on that database roundtrip, for something like `sql, *params = SomeClass.build_sql_references()`,
        you can check for `sql == fastorm.SQL_DO_NOTHING` and then not do the database query.
        """
        # references to other tables
        references_types: cls._GET_FIELDS_REFERENCES_TYPE = {
            # everything with a different table will have more than one type in there.
            long_key: field_typehint for long_key, field_typehint in cls.get_fields_references(recursive=True).items()
            if len(field_typehint.types) > 1
        }
        if not references_types:
            # noinspection PyRedundantParentheses
            return (SQL_DO_NOTHING,)
        # end if

        import dataclasses
        @dataclasses.dataclass
        class Holder(object):
            referenced_table: str
            table_fields: List[str]
            referenced_table_fields: List[str]
        # end class

        data_per_field: Dict[str, Holder] = {}
        for current_table_field, field_typehint in references_types.items():
            short_key = field_typehint.unflattened_field
            assert issubclass(field_typehint.referenced_type, FastORM)
            new_holder = Holder(
                referenced_table=typing.cast(Type[FastORM], field_typehint.referenced_type).get_name(),
                table_fields=[current_table_field],
                referenced_table_fields=[field_typehint.referenced_field_sql],
            )
            if short_key not in data_per_field:
                data_per_field[short_key] = new_holder
            else:
                holder = data_per_field[short_key]
                assert holder.referenced_table == new_holder.referenced_table
                holder.table_fields += new_holder.table_fields
                holder.referenced_table_fields += new_holder.referenced_table_fields
            # end if
        # end for

        index_lines = []
        reference_lines = []
        current_table = cls.get_name()
        for short_key, holder in data_per_field.items():
            if len(holder.table_fields) == 0 or len(holder.referenced_table_fields) == 0:
                raise ValueError('Shouldn\'t get 0 references...')
            # end if
            # noinspection SqlNoDataSourceInspection,SqlResolve
            for current_table_field in holder.table_fields:
                index_lines.append(
                    f'CREATE INDEX "idx_{current_table}___{current_table_field}" ON "{current_table}" ("{current_table_field}");'
                )
            # end for
            referenced_table = holder.referenced_table
            current_table_fields = ', '.join(f'"{current_table_field}"' for current_table_field in holder.table_fields)
            referenced_table_fields = ', '.join(f'"{current_referenced_table_field}"' for current_referenced_table_field in holder.referenced_table_fields)
            constraint_suffix = short_key if len(holder.table_fields) > 1 else f'{short_key}__{holder.referenced_table_fields[0]}'
            # noinspection SqlNoDataSourceInspection,SqlResolve
            reference_lines.append(
                f'ALTER TABLE "{current_table}" ADD CONSTRAINT "fk_{current_table}___{constraint_suffix}" FOREIGN KEY ({current_table_fields}) REFERENCES "{referenced_table}" ({referenced_table_fields}) ON DELETE CASCADE;'
            )
        # end for
        sql_lines: List[str] = []
        sql_lines.extend(index_lines)
        sql_lines.extend(reference_lines)
        sql = "\n".join(
            sql_lines
        )
        # noinspection PyRedundantParentheses
        return (sql,)
        # end if
    # end def

    @classmethod
    def match_type(
        cls,
        type_hint: TYPEHINT_TYPE,
        *,
        is_automatic_field: Optional[bool] = None,
        key: Optional[str] = None,
        is_outer_call: bool = True
    ) -> Tuple[bool, str]:
        """
        Processes a type hint to produce a CREATE TABLE sql segment of the type of that type hint and if it's optional..

            >>> class Example(FastORM):
            ...   foo: Optional[int]
            ...

            >>> type_hints = typing.get_type_hints(Example)
            >>> Example.match_type(type_hints['foo'], is_automatic_field=False)
            (True, 'BIGINT')

        """
        is_union_type = check_is_new_union_type(type_hint)
        if isinstance(type_hint, typing.ForwardRef):
            if not type_hint.__forward_evaluated__:
                raise ValueError(
                    f'The typehint of {cls.__name__}.{key} is still a unresolved ForwardRef. You should probably call {cls.__name__}.update_forward_refs() after the class it is pointing to is defined.'
                )
            # end if
            type_hint = type_hint.__forward_value__
        # end if

        if hasattr(type_hint, '__origin__') or is_union_type:
            if check_is_annotated_type(type_hint):
                # https://stackoverflow.com/q/68275615/3423324#what-is-the-right-way-to-check-if-a-type-hint-is-annotated
                actual_type = type_hint.__origin__  # str or wherever was the first parameter.
                metadata = type_hint.__metadata__
                if not isinstance(metadata, (tuple, list)):
                    metadata = (metadata,)
                # end if
                if AutoincrementType in metadata:
                    is_automatic_field = True
                # end if
                is_optional, sql_type = cls.match_type(
                    actual_type, is_automatic_field=is_automatic_field, is_outer_call=False
                )
                return is_optional, sql_type
            # end if

            origin = type_hint.__origin__ if hasattr(type_hint, '__origin__') else type(type_hint)
            is_union_type = check_is_new_union_type(origin)
            if is_union_type or origin in (typing.Optional, typing.Union):  # Optional is an special union, too
                is_optional, sql_type = cls.process_unions(is_automatic_field, key, type_hint)
            elif isinstance(origin, typing.List) or issubclass(origin, list):
                list_params = type_hint.__args__
                if len(list_params) != 1:  # list has one type
                    raise TypeError(
                        'List with more than one type parameter.', type_hint, list_params
                    )
                # end if
                the_type = list_params[0]
                try:
                    # we will now recursively go into that list.
                    # if it is like `list[list[list[int]]] it will succeed as INT,
                    # and for those 3 lists the [] will be added 3 times, resulting in INT[][][]
                    # If any of those inner lists aren't a compatible type (TypeError),
                    # e.g. list[list[Union[str, int]]], we have to use a json dict instead.
                    _, sql_type = cls.match_type(
                        the_type, is_automatic_field=is_automatic_field, is_outer_call=False
                    )
                    sql_type = "".join((sql_type, "[]"))  # append '[]' to the sql_type
                    return False, sql_type  # the list itself can't be optional, that has to be done by an outer Optional[].
                except TypeError as e:
                    if not is_outer_call:
                        # make sure we don't end up with JSONB[] for list[list[Union[str, int]]],
                        # only the outer one should migrate to json.
                        raise e
                    # end if
                    logger.debug('Could not parse as a single type list (e.g. INT[][]), now will be a json field.', exc_info=True)
                    return False, cls._COLUMN_TYPES[dict]
                # end try
            elif isinstance(origin, typing.Tuple) or issubclass(origin, builtins.tuple):
                tuple_params = type_hint.__args__
                if len(tuple_params) == 0:  # list has one type
                    raise TypeError(
                        'Tuple has no parameters.', type_hint, tuple_params
                    )
                # end if

                # check if all types of the tuple are the same, so we can use a list
                first_type = tuple_params[0]
                if all(first_type == x for x in tuple_params[1:]):
                    # we hope this will give us something like  INT, TEXT, FLOAT, etc.
                    _, sql_type = cls.match_type(
                        first_type, is_automatic_field=is_automatic_field, is_outer_call=False
                    )
                    sql_type = "".join((sql_type, "[]"))  # append '[]' to the sql_type
                    return False, sql_type  # the tuple itself can't be optional, that has to be done by an outer Optional[].
                # end if

                # so the types are all over the place, so we will have to fallback to json.
                sql_type = cls._COLUMN_TYPES[dict]
                return False, sql_type  # the list itself can't be optional, that has to be done by an outer Optional[].
            else:
                raise ValueError('Enclosed by an unknown type', origin, f'key={key!r}')
            # end case
        elif isinstance(type_hint, ModelField):
            is_optional = type_hint.allow_none
            # is_optional = type_hint.allow_none and (type_hint.shape != SHAPE_SINGLETON or not type_hint.sub_fields)
            try:
                subtype_is_optional, sql_type = cls.match_type(
                    type_hint=type_hint.type_, is_automatic_field=is_automatic_field, key=key, is_outer_call=False,
                )
            except TypeError as e:
                if not is_outer_call:
                    # make sure we don't end up with JSONB[] for list[list[Union[str, int]]],
                    # only the outer one should migrate to json.
                    raise e
                # end if
                logger.debug(
                    'Could not parse as a single type list (e.g. INT[][]), now will be a json field.', exc_info=True
                )
                return False, cls._COLUMN_TYPES[dict]
            # end try

            # pydantic makes t6_2: str = None  to be  t6_2: Optional[str] = None, couldn't find a way to detect only the first variant.
            # if is_optional and type_hint.field_info.default is None:
            #     # e.g. t6_2: str = None
            #     assert type_hint.field_info.default is None
            #     assert not isinstance(type_hint.field_info.default, UndefinedType)
            #     raise ValueError("You can't have an non-optional type default to None")
            # # end if

            if type_hint.outer_type_ == type_hint.type_:
                # e.g. for str,  typehint.outer_type_ is str
                #      for str,  typehint.type_       is str
                # but also for Optional[str] it is  both  str!
                return is_optional, sql_type
            if check_is_generic_alias(type_hint.outer_type_) and hasattr(type_hint.outer_type_, '__origin__'):
                # e.g. for list[int],  typehint.outer_type_ is List[int], thus having a .__origin__ == list
                #      for list[int],  typehint.type_       is int
                # isinstance(list[int], GenericAlias) == True

                wrapper_class = type_hint.outer_type_.__origin__  # e.g. list if we had List[int].
                if issubclass(wrapper_class, list):
                    sql_type = "".join([sql_type, "[]"])
                # end if
            elif check_is_annotated_type(type_hint.outer_type_):
                return cls.match_type(
                    type_hint.outer_type_, is_automatic_field=is_automatic_field, is_outer_call=False
                )
            # end if
        else:
            is_optional = False
            sql_type = cls._match_type(type_hint, automatic=is_automatic_field)  # fails anyway if not in the list above
        # end case
        return is_optional, sql_type

    @classmethod
    def process_unions(cls, is_automatic_field, key, type_hint):
        union_params = type_hint.__args__[:]  # this was __union_params__ in python3.5, but __args__ in 3.6+
        if not isinstance(union_params, (list, tuple)):
            raise TypeError(
                f'Union type for key {key} has unparsable params.', union_params,
            )
        # end if
        if NoneType in union_params:
            is_optional = True
            union_params = [param for param in union_params if not issubclass(param, NoneType)]
        else:
            is_optional = False
        # end if
        if len(union_params) == 0:
            raise TypeError(
                f'Union with no (non-None) type(s) at key {key}.', type_hint.__args__,
            )
        # end if

        # Check that we don't have Union[int] or something similar with just one element (remaining).
        if len(union_params) == 1:
            additional_is_optional, sql_type = cls.match_type(
                union_params[0], is_automatic_field=is_automatic_field, is_outer_call=False
            )
            if additional_is_optional:
                is_optional = True
            # end if
            return is_optional, sql_type
        # end if

        special_union_types = [union_param for union_param in union_params if failsafe_issubclass(union_param, FastORM)]
        if len(special_union_types) > 1:
            raise TypeError(
                f'Found more than one type of FastORM at key {key}.', type_hint.__args__,
            )
        # end if
        if len(special_union_types) == 0:
            raise TypeError(
                f"Didn't find a FastORM type at key {key}.", type_hint.__args__,
            )
        first_union_type = special_union_types[0]
        other_union_types = [union_param for union_param in union_params if union_param != first_union_type]
        if all(first_union_type == x for x in other_union_types):
            # that would happen if somehow not deduplicated properly
            pass
        else:
            pk_type = tuple(first_union_type.get_primary_keys_type_annotations(ref_as_union_with_pk=False).values())
            pk_type = pk_type[0] if len(pk_type) == 1 else typing.Tuple.__getitem__(pk_type)

            remaining_params = other_union_types[:]
            for remaining_param in remaining_params:
                if ModelMetaclassFastORM.is_generic_alias_equal(pk_type, remaining_param):
                    remaining_params.remove(remaining_param)
                # end if
            # end for
            pk_type_ref = tuple(first_union_type.get_primary_keys_type_annotations(ref_as_union_with_pk=True).values())
            pk_type_ref = pk_type_ref[0] if len(pk_type_ref) == 1 else typing.Tuple.__getitem__(pk_type)

            if not ModelMetaclassFastORM.is_generic_alias_equal(pk_type, pk_type_ref):  # pk_type, pk_type_ref
                # they are different
                for remaining_param in remaining_params:
                    if ModelMetaclassFastORM.is_generic_alias_equal(pk_type_ref, remaining_param):
                        remaining_params.remove(remaining_param)
                    # end if
                # end for
            # end if

            if len(remaining_params) > 0:
                raise TypeError(
                    f'Union with more than one type at key {key}.', union_params,
                )
            # end if
        # end if
        additional_is_optional, sql_type = cls.match_type(
            first_union_type, is_automatic_field=is_automatic_field, is_outer_call=False
        )
        if additional_is_optional:
            is_optional = True
        # end if
        return is_optional, sql_type
    # end def

    _CLASS_SERIALIZERS = {
        # # class: callable(data) -> json
        # # E.g.:
        # TgBotApiObject: lambda obj: return obj._raw if hasattr(obj, '_raw') and obj._raw else obj.to_array()
    }

    @classmethod
    async def create_connection(cls, database_url) -> Connection:
        # https://magicstack.github.io/asyncpg/current/usage.html#example-automatic-json-conversion
        conn = await asyncpg.connect(database_url)
        return await cls._set_up_connection(conn=conn)
    # end def

    @classmethod
    async def create_connection_pool(cls, database_url) -> Pool:
        # https://magicstack.github.io/asyncpg/current/usage.html#example-automatic-json-conversion
        return await asyncpg.create_pool(database_url, setup=cls._set_up_connection)
    # end def

    @classmethod
    async def _set_up_connection(cls, conn: Connection):
        """
        Sets up a connection to properly do datetime and json decoding.

        Prepares writing datetimes (as ISO format) and class instances as json if they have a `.to_dict()`, `.to_array()` function.
        An easy way to add your is by having a `.to_json()` function like above or
        appending your class to `_CLASS_SERIALIZERS` like so:
        ```py
        # anywhere in your code, to be run once
        FastORM._CLASS_SERIALIZERS[SomeClass] = lambda obj: obj.do_something()
        ```
        :param conn:
        :return:
        """
        import json

        def decoder_with_empty(text):
            if text.strip() == '':
                return None
            # end if
            return json.loads(text)
        # end def

        def json_dumps(obj):
            logger.debug(f'Encoding to JSON: {obj!r}')

            def my_converter(o):
                if isinstance(o, datetime.datetime):
                    return o.isoformat()
                # end def
                if hasattr(o, 'to_array'):
                    return o.to_array()  # TgBotApiObject from pytgbot
                # end def
                if hasattr(o, 'to_dict'):
                    return o.to_dict()
                # end def

                # check _CLASS_SERIALIZERS,
                # a easy way to add your own by writing FastORM._CLASS_SERIALIZERS[Class] = lambda obj: obj
                for type_to_check, callable_function in cls._CLASS_SERIALIZERS.items():
                    if isinstance(o, type_to_check):
                        return callable_function(o)
                    # end if
                # end for
            # end def
            return json.dumps(obj, default=my_converter)
        # end def

        for sql_type in ('json', 'jsonb'):
            await conn.set_type_codec(
                sql_type,
                encoder=json_dumps,
                decoder=decoder_with_empty,
                schema='pg_catalog'
            )
        # end for
        return conn
    # end def

    @classmethod
    async def get_connection(cls, database_url):
        """
        Deprecated, use `FastORM.create_connection(…)` instead.
        """
        logger.warning(
            'The use of FastORM.get_connection(…) is deprecated, use FastORM.create_connection(…)` instead.',
            exc_info=True,
        )
        return await cls.create_connection(database_url=database_url)
    # end def
# end class


class FastORM(_BaseFastORM, metaclass=ModelMetaclassFastORM):
    _table_name: str  # database table name we run queries against
    _ignored_fields: List[str]  # fields which never are intended for the database and will be excluded in every operation. (So are all fields starting with an underscore)
    _automatic_fields: List[str]  # fields the database fills in, so we will ignore them on INSERT.
    _primary_keys: List[str]  # this is how we identify ourself.
    _database_cache: Dict[str, JSONType] = PrivateAttr()  # stores the last known retrieval, so we can run UPDATES after you changed parameters.
    __selectable_fields: List[str] = PrivateAttr()  # cache for `cls.get_sql_fields()`
    __fields_typehints: Dict[bool, Dict[str, FieldInfo[ModelField]]] = PrivateAttr()  # cache for `cls.get_fields_typehint()`
    __fields_references: Dict[bool, Dict[str, FieldInfo[ModelField]]] = PrivateAttr()  # cache for `cls.get_fields_typehint()`
    __original__annotations__: Dict[str, Any]  # filled by the metaclass, before we do modify the __annotations__
    __original__fields__: Dict[str, ModelField]  # filled by the metaclass, before we do modify the __fields__
# end class


class AutoincrementType(object):
    """
    Below there will be a singleton called Autoincrement.
    It can be used in the following two ways, they are the same:

        >>. Autoincrement = AutoincrementType()
        >>. class Foo(FastORM):
        ...   id_a: int = Field(default_factory=Autoincrement, other_field_parameters=...)
        ...   id_b: int = Autoincrement(other_field_parameters=...)

    In other words, the following two are doing the same:

        >>. example_1 = Autoincrement(default=None)
        >>. example_2 = Field(default_factory=Autoincrement)

        >>. from pydantic.fields import FieldInfo
        >>. isinstance(example_1, FieldInfo)
        True
        >>. isinstance(example_2, FieldInfo)
        True

        >>. Autoincrement().__dir__() == Field(default_factory=Autoincrement).__dir__()
        True

    """

    def __init__(self):
        self.__name__ = "fastorm.Autoincrement"  # some internals of Field want to know that.
    # end def

    @typing.overload
    def __call__(
        self,
        default: Any = Undefined,
        *,
        default_factory: Optional[NoArgAnyCallable] = None,
        alias: str = None,
        title: str = None,
        description: str = None,
        const: bool = None,
        gt: float = None,
        ge: float = None,
        lt: float = None,
        le: float = None,
        multiple_of: float = None,
        min_items: int = None,
        max_items: int = None,
        min_length: int = None,
        max_length: int = None,
        allow_mutation: bool = True,
        regex: str = None,
        **extra: Any,
    ) -> Any:
        pass
    # end def

    def __call__(self, *args, **kwargs) -> None:
        if not args and not kwargs:
            # For the use in `Field(default_factory=Autoincrement)`, we will be called without parameters.
            # so we return the default value this object should then have, that is `None`.
            # It would be cooler, but we can't return Autoincrement as that would be incompatible with the field's type.
            return None
        # end if

        # so it's not the `Field(default_factory=Autoincrement)` calling us with zero parameters
        assert 'default_factory' not in kwargs
        kwargs['default_factory'] = Autoincrement
        return Field(*args, **kwargs)
    # end def
# end class


Autoincrement = AutoincrementType()
