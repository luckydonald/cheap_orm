#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from dataclasses import dataclass
import typing

try:
    from pydantic.fields import ModelField, FieldInfo
except ImportError:
    from pydantic.v1.fields import ModelField, FieldInfo
# end try
from luckydonaldUtils.logger import logging

__author__ = 'luckydonald'

logger = logging.getLogger(__name__)
if __name__ == '__main__':
    logging.add_colored_handler(level=logging.DEBUG)
# end if


def __getitem__(self, key):
    return getattr(self, key)
# end def


def __iter__(self):
    return iter(dataclasses.astuple(self))
# end def


FIELD_REFERENCE_TYPE = typing.TypeVar("FIELD_REFERENCE_TYPE")

FIELD_REFERENCE_ITEM_WITH_TYPE = typing.Union[type, typing.Type['FastORM']]   # the FIELD_REFERENCE_TYPE of TYPE type()
FIELD_REFERENCE_ITEM_WITH_TYPE_HINT = FieldInfo  # the FIELD_REFERENCE_TYPE of TYPE pydantic.typehint


@dataclass
class FieldItem(typing.Generic[FIELD_REFERENCE_TYPE]):
    field: str
    type_: FIELD_REFERENCE_TYPE

    __getitem__ = __getitem__  # reuse, as it's the same function basically
    __iter__ = __iter__  # reuse, as it's the same function basically
# end class


@dataclass
class FieldInfo(typing.Generic[FIELD_REFERENCE_TYPE]):
    is_primary_key: bool
    types: typing.List[FieldItem[FIELD_REFERENCE_TYPE]]

    @property
    def is_reference(self) -> bool:
        return len(self.types) > 1
    # end def

    @property
    def resulting_type(self) -> FIELD_REFERENCE_TYPE:
        """
        The last type in the type resolving list.

        That means this is the final type this field is actually having after resolving all the table references.
        TL;DR: flattened to the end type
        """
        return self.types[-1].type_
    # end def

    @property
    def unflattened_field(self) -> str:
        """
        The first key, i.e. the unflattened one of the current class
        """
        return self.types[0].field
    # end def

    @property
    def referenced_type(self) -> FIELD_REFERENCE_TYPE:
        """
        The first type in the type resolving list.

        That means this is the first type, pointing to either the actual type if there's no reference or the table it references to
        TL;DR: no flattening to the end type
        """
        return self.types[0].type_
    # end def

    @property
    def referenced_field(self) -> str:
        """
        The first type in the type resolving list.

        Not the one of the current class.
        That means this is the first type, pointing to either the actual field if there's no reference or the field in the table it references to

        TL;DR: no flattening to the end field
        """
        return self.types[1].field
    # end def

    @property
    def referenced_field_sql(self) -> str:
        """
        Builds the name a sql fields needs to point the reference to,
        based on the names of the remaining field reference chain.
        :return:
        """
        return '__'.join(type_.field for type_ in self.types[1:])
    # end def

    __getitem__ = __getitem__  # reuse, as it's the same function basically
    __iter__ = __iter__  # reuse, as it's the same function basically
# end class


SQL_FIELD_META_VALUE_TYPE = typing.TypeVar("SQL_FIELD_META_VALUE_TYPE")


@dataclass
class SqlFieldMeta(typing.Generic[SQL_FIELD_META_VALUE_TYPE]):
    sql_name: str
    field_name: str
    type_: FieldInfo[typing.Union[typing.Type]]
    field: FieldInfo[ModelField]
    value: SQL_FIELD_META_VALUE_TYPE

    def __str__(self):
        return (
            f'{self.__class__.__name__}('
            f'value={self.value!r}, '
            f'sql_name={self.sql_name!r}, '
            f'field_name={self.field_name!r}, '
            f'type_={self.type_!r}, '
            # f'field={self.field!r}'
            r')'
        )
# end class
