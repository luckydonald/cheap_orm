#!/usr/bin/env python3
# -*- coding: utf-8 -*-
__author__ = 'luckydonald'

import dataclasses
from typing import List, Dict, Any, Optional, Tuple, Type, get_type_hints, Union, TypeVar

from asyncpg import Connection
from luckydonaldUtils.exceptions import assert_type_or_raise
from luckydonaldUtils.logger import logging

from luckydonaldUtils.typing import JSONType
from typeguard import check_type

VERBOSE_SQL_LOG = True
CLS_TYPE = TypeVar("CLS_TYPE")


logger = logging.getLogger(__name__)
if __name__ == '__main__':
    logging.add_colored_handler(level=logging.DEBUG)
# end if


class CheapORM(object):
    _table_name: str  # database table name we run queries against
    _ignored_fields: List[str]  # fields which never are intended for the database and will be excluded in every operation. (So are all fields starting with an underscore)
    _automatic_fields: List[str]  # fields the database fills in, so we will ignore them on INSERT.
    _primary_keys: List[str]  # this is how we identify ourself.
    _database_cache: Dict[str, JSONType]  # stores the last known retrieval, so we can run UPDATES after you changed parameters.
    __selectable_fields: List[str]  # cache for `cls.get_sql_fields()`

    def __init__(self):
        self.__post_init__()
    # end def

    def __post_init__(self):
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
        return dataclasses.asdict(self)
    # end def

    @classmethod
    def get_fields(cls) -> List[str]:
        return [f.name for f in dataclasses.fields(cls)]
    # end if

    @classmethod
    def get_sql_fields(cls) -> List[str]:
        key = f'_{cls.__name__!s}__selectable_fields'
        if getattr(cls, key, None) is None:
            setattr(cls, key, [field for field in cls.get_fields() if not field.startswith('_')])
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
        _table_name = getattr(cls, '_table_name')
        return f'"{_table_name}"'
    # end def

    def build_sql_insert(
        self, *, ignore_setting_automatic_fields: bool, on_conflict_upsert_field_list: Optional[List[str]]
    ) -> Tuple[str, Any]:
        own_keys = self.get_fields()
        _table_name = getattr(self, '_table_name')
        _ignored_fields = getattr(self, '_ignored_fields')
        _automatic_fields = getattr(self, '_automatic_fields')
        assert_type_or_raise(_table_name, str, parameter_name='self._table_name')
        assert_type_or_raise(_ignored_fields, list, parameter_name='self._ignored_fields')
        assert_type_or_raise(_automatic_fields, list, parameter_name='self._automatic_fields')
        _ignored_fields += ['_table_name', '_ignored_fields']

        placeholder = []
        values: List[JSONType] = []
        keys = []
        upsert_fields = {}  # key is field name, values is the matching placeholder_index.
        placeholder_index = 0
        primary_key_index = 0
        for key in own_keys:
            if key in _ignored_fields:
                continue
            # end if
            is_automatic_field = None
            if ignore_setting_automatic_fields or on_conflict_upsert_field_list:
                is_automatic_field = key in _automatic_fields
            if ignore_setting_automatic_fields and is_automatic_field:
                continue
            # end if
            value = getattr(self, key)
            placeholder_index += 1
            placeholder.append(f'${placeholder_index}')
            if isinstance(value, dict):
                pass
                # value = json.dumps(value)
            # end if
            if isinstance(value, CheapORM):
                # we have a different table in this table, so we probably want to go for it's `id` or whatever the primary key is.
                # if you got more than one of those PKs, simply specify them twice for both fields.
                value = value.get_primary_keys_values()[primary_key_index]
                primary_key_index += 1
            # end if

            values.append(value)
            keys.append(f'"{key}"')

            if on_conflict_upsert_field_list and not is_automatic_field:
                upsert_fields[key] = placeholder_index
            # end if
        # end if

        sql = f'INSERT INTO "{_table_name}" ({",".join(keys)})\n VALUES ({",".join(placeholder)})'
        if on_conflict_upsert_field_list and upsert_fields:
            upsert_sql = ', '.join([f'"{key}" = ${placeholder_index}' for key, placeholder_index in upsert_fields.items()])
            upsert_fields_sql = ', '.join([f'"{field}"' for field in on_conflict_upsert_field_list])
            sql += f'\n ON CONFLICT ({upsert_fields_sql}) DO UPDATE SET {upsert_sql}'
        # end if
        if _automatic_fields:
            automatic_fields_sql = ', '.join([f'"{key}"' for key in _automatic_fields])
            sql += f'\n RETURNING {automatic_fields_sql}'
        # end if
        sql += '\n;'
        return (sql, *values)
    # end def

    @classmethod
    async def get(cls: Type[CLS_TYPE], conn: Connection, **kwargs) -> Optional[CLS_TYPE]:
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
    async def select(cls: Type[CLS_TYPE], conn: Connection, **kwargs) -> List[CLS_TYPE]:
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
    def build_sql_select(cls, **kwargs):
        _ignored_fields = getattr(cls, '_ignored_fields')
        typehints: Dict[str, Any] = get_type_hints(cls)
        non_ignored_fields = [field for field in cls.get_fields() if field not in _ignored_fields]
        fields = ','.join([
            f'"{field}"'
            for field in non_ignored_fields
            if not field.startswith('_')
        ])
        where_index = 0
        where_parts = []
        where_values = []
        # noinspection PyUnusedLocal
        where_wolf = None
        for key, value in kwargs.items():
            if key not in non_ignored_fields:
                raise ValueError(f'key {key!r} is not a non-ignored field!')
            # end if
            assert not isinstance(value, CheapORM)
            # if isinstance(value, HelpfulDataclassDatabaseMixin):
            #     # we have a different table in this table, so we probably want to go for it's `id` or whatever the primary key is.
            #     # if you got more than one of those PKs, simply specify them twice for both fields.
            #     value = value.get_primary_keys_values()[primary_key_index]
            #     primary_key_index += 1
            # # end if
            where_index += 1
            is_in_list_clause = cls._param_is_list_of_multiple_values(key, value, typehints[key])
            if is_in_list_clause:
                assert isinstance(value, (list, tuple))
                assert len(value) >= 1
                if len(value) == 1:
                    # single element list -> normal where is fine -> so we go that route with it.
                    value = value[0]
                    is_in_list_clause = False
                # end if
            # end if
            if not is_in_list_clause:  # no else-if as is_in_list_clause could be set False again.
                where_parts.append(f'"{key}" = ${where_index}')
                where_values.append(value)
            else:  # is_in_list_clause is True
                where_part = ''
                for actual_value in value:
                    where_part += f'${where_index},'
                    where_values.append(actual_value)
                    where_index += 1
                # end for
                where_index -= 1  # we end up with incrementing once too much
                where_parts.append(f'"{key}" IN ({where_part.rstrip(",")})')
            # end if
        # end if

        # noinspection SqlResolve
        sql = f'SELECT {fields} FROM "{cls._table_name}" WHERE {" AND ".join(where_parts)}'
        return (sql, *where_values)
    # end def

    async def insert(
        self, conn: Connection, *, ignore_setting_automatic_fields: bool,
        on_conflict_upsert_field_list: Optional[List[str]],
        write_back_automatic_fields: bool,
    ):
        """

        :param conn: Database connection to run at.
        :param ignore_setting_automatic_fields:
            Skip setting fields marked as automatic, even if you provided.
            For example if the id field is marked automatic, as it's an autoincrement int.
            If `True`, setting `id=123` (commonly `id=None`) would be ignored, and instead the database assignes that value.
            If `False`, the value there will be written to the database.
        :param on_conflict_upsert_field_list:
        :param write_back_automatic_fields: Apply the automatic fields back to this object.
                                            Ignored if `ignore_setting_automatic_fields` is False.
        :return:
        """
        fetch_params = self.build_sql_insert(
            ignore_setting_automatic_fields=ignore_setting_automatic_fields,
            on_conflict_upsert_field_list=on_conflict_upsert_field_list,
        )
        self._database_cache_overwrite_with_current()
        _automatic_fields = getattr(self, '_automatic_fields')
        if VERBOSE_SQL_LOG:
            fetch_params_debug = "\n".join([f"${i}={param!r}" for i, param in enumerate(fetch_params)][1:])
            logger.debug(f'INSERT query for {self.__class__.__name__}\nQuery:\n{fetch_params[0]}\nParams:\n{fetch_params_debug!s}')
        else:
            logger.debug(f'INSERT query for {self.__class__.__name__}: {fetch_params!r}')
        # end if
        updated_automatic_values_rows = await conn.fetch(*fetch_params)
        logger.debug(f'INSERTed {self.__class__.__name__}: {updated_automatic_values_rows} for {self}')
        assert len(updated_automatic_values_rows) == 1
        updated_automatic_values = updated_automatic_values_rows[0]
        if ignore_setting_automatic_fields and write_back_automatic_fields:
            for field in _automatic_fields:
                assert field in updated_automatic_values
                setattr(self, field, updated_automatic_values[field])
                self._database_cache[field] = updated_automatic_values[field]
            # end for
        # end if
    # end def

    def build_sql_update(self):
        """
        Builds a prepared statement for update.
        :return:
        """
        own_keys = self.get_fields()
        _table_name = getattr(self, '_table_name')
        _ignored_fields = getattr(self, '_ignored_fields')
        _automatic_fields = getattr(self, '_automatic_fields')
        _database_cache = getattr(self, '_database_cache')
        _primary_keys = getattr(self, '_primary_keys')
        assert_type_or_raise(_table_name, str, parameter_name='self._table_name')
        assert_type_or_raise(_ignored_fields, list, parameter_name='self._ignored_fields')
        assert_type_or_raise(_automatic_fields, list, parameter_name='self._automatic_fields')
        assert_type_or_raise(_database_cache, dict, parameter_name='self._database_cache')
        assert_type_or_raise(_primary_keys, list, parameter_name='self._primary_keys')

        # SET ...
        update_values: Dict[str, Any] = {}
        for key in own_keys:
            if key.startswith('_') or key in _ignored_fields:
                continue
            # end if
            value = getattr(self, key)
            if key not in _database_cache:
                update_values[key] = value
            # end if
            if _database_cache[key] != value:
                update_values[key] = value
            # end if
        # end if

        # UPDATE ... SET ... WHERE ...
        placeholder_index = 0
        other_primary_key_index = 0
        values: List[Any] = []
        update_keys: List[str] = []  # "foo" = $1
        for key, value in update_values.items():
            value = getattr(self, key)
            placeholder_index += 1
            if isinstance(value, CheapORM):
                # we have a different table in this table, so we probably want to go for it's `id` or whatever the primary key is.
                # if you got more than one of those PKs, simply specify them twice for both fields.
                value = value.get_primary_keys_values()[other_primary_key_index]
                other_primary_key_index += 1
            # end if

            values.append(value)
            update_keys.append(f'"{key}" = ${placeholder_index}')
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
        sql += f' SET {",".join(update_keys)}'
        sql += f' WHERE {",".join(primary_key_where)}'
        sql += '\n;'
        return (sql, *values)
    # end def

    async def update(self, conn: Connection):
        if not getattr(self, '_database_cache', None):
            return  # nothing to do.
        # end if
        fetch_params = self.build_sql_update()
        logger.debug(f'UPDATE query for {self.__class__.__name__}: {fetch_params!r}')
        update_status = await conn.execute(*fetch_params)
        logger.debug(f'UPDATEed {self.__class__.__name__}: {update_status} for {self}')
        self._database_cache_overwrite_with_current()
    # end if

    def build_sql_delete(self):
        _table_name = getattr(self, '_table_name')
        _primary_keys = getattr(self, '_primary_keys')
        _ignored_fields = getattr(self, '_ignored_fields')
        _database_cache = getattr(self, '_database_cache')
        assert_type_or_raise(_table_name, str, parameter_name='self._table_name')
        assert_type_or_raise(_ignored_fields, list, parameter_name='self._ignored_fields')
        assert_type_or_raise(_primary_keys, list, parameter_name='self._primary_keys')
        assert_type_or_raise(_database_cache, dict, parameter_name='self._database_cache')

        # DELETE FROM "name" WHERE pk...
        where_values = []
        placeholder_index = 0
        primary_key_parts: List[str] = []  # "foo" = $1
        for primary_key in _primary_keys:
            if primary_key in _database_cache:
                value = _database_cache[primary_key]
            else:
                value = getattr(self, primary_key)
            # end if
            placeholder_index += 1
            primary_key_parts.append(f'"{primary_key}" = ${placeholder_index}')
            where_values.append(value)
        # end if
        logger.debug(f'Fields to DELETE for selector {primary_key_parts!r}: {where_values!r}')

        # noinspection SqlWithoutWhere,SqlResolve
        sql = f'DELETE FROM "{_table_name}"\n'
        sql += f' WHERE {",".join(primary_key_parts)}'
        sql += '\n;'
        return (sql, *where_values)
    # end def

    async def delete(self, conn: Connection):
        fetch_params = self.build_sql_delete()
        logger.debug(f'DELETE query for {self.__class__.__name__}: {fetch_params!r}')
        delete_status = await conn.execute(*fetch_params)
        logger.debug(f'DELETEed {self.__class__.__name__}: {delete_status} for {self}')
        self._database_cache_remove()
    # end if

    def clone(self: CLS_TYPE) -> CLS_TYPE:
        return self.__class__(**self.as_dict())
    # end if

    def get_primary_keys(self) -> Dict[str, Any]:
        return {k: v for k, v in self.as_dict().items() if k in self._primary_keys}
    # end def

    def get_primary_keys_values(self):
        return list(self.get_primary_keys().values())
    # end def

    @classmethod
    def from_row(cls, row, is_from_database: bool = False):
        # noinspection PyArgumentList
        instance = cls(*row)
        instance._database_cache_overwrite_with_current()
        return instance
    # end def

    @staticmethod
    def dataclass(other_cls):
        """
        :param other_cls:
        :return:
        """
        body = """

        """
        _primary_keys = getattr(other_cls, '_primary_keys')
        fields = [
             f for f in dataclasses.fields(other_cls)
             if f.init and f.name in _primary_keys
        ]
        func_args = ','.join([dataclasses._init_param(f) for f in fields])
        call_args = ','.join([f'{f.name}={f.name}' for f in fields])
        locals = {f'_type_{f.name}': f.type for f in fields}
        locals[other_cls.__name__] = other_cls
        import builtins
        globals = {}
        globals['__builtins__'] = builtins
        # Compute the text of the entire function.
        txt = f'async def get(self, {func_args}) -> {other_cls.__name__}:\n await self.get({call_args})'
        logger.debug(f'setting up `get`: {txt!r}')
        function = _create_func('get', txt, globals, locals)
        setattr(other_cls, 'get', function)
        return other_cls
    # end def

    @classmethod
    def _param_is_list_of_multiple_values(cls, key: str, value: Any, typehint: Any):
        """
        If a value is multiple times of what was defined.
        :param key:
        :param value:
        :param typehint:
        :return: True if the `value` is a list of tuple of arguments satisfying the `typehint`.
        """
        if not isinstance(value, (list, tuple)):
            # we don't have a list -> can't be multiple values
            # this is a cheap check preventing most of the values.
            return False
        # end if

        original_type_fits = False
        listable_type_fits = False
        try:
            check_type(argname=key, value=value, expected_type=typehint)
            original_type_fits = True  # the original was already compatible
        except TypeError as e:
            pass
        # end if
        try:
            check_type(argname=key, value=value, expected_type=Union[Tuple[typehint], List[typehint]])
            listable_type_fits = True  # the original was already compatible
        except TypeError as e:
            pass
        # end if

        logger.debug(f'type fitting analysis: original: {original_type_fits}, tuple/list: {listable_type_fits}')
        if not listable_type_fits:
            # easy one,
            # so our list/tuple check doesn't match,
            # so it isn't compatible.
            return False
        # end if
        if not original_type_fits:
            # tuple/list one fits
            # but the original one does not.
            # pretty confident we have a list/tuple of a normal attribute and can do a `WHERE {key} IN ({",".join(value)})`
            return True
        # end if

        # else -> listable_type_fits == True, original_type_fits == True
        # ooof, so that one would have already be accepted by the original type...
        # That's a tough one...
        # we err to the side of caution
        logger.warn(f'Not quite sure if it fits or not, erring to the side of caution and assuming single parameter.')
        return False
    # end def
# end class


def _create_func(name, txt, globals, locals):
    exec(txt, globals, locals)
    return locals[name]
# end def