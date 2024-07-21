> ### Known bugs:
> 1. In Python versions **before Python 3.10**, the automated **CREATE TABLE statement** (`build_sql_create(…)` thus also `create_table(…)`) will have problems with referencing other tables and list types:     
>   That means, `list[int]` will be missing an array at the end (`BIGINT[]` will be only `BIGINT`, `BIGINT[][][]` will be only `BIGINT[][]`) and
>   referencing other tables as part of an optional segment will result in an `JSONB` field instead of the referencing fields.   
>   **Workaround**: Change the type of the field after creation in python versions if you must use both 3.10 and the `create_table(…)` (or `build_sql_create(…)`) function
> - That's all we know of.


> ### Known limitations:
> 1. Currently references are not working correctly if you provide a union with it and the underlying primary type (or type tuple.)
>    **Solution**: Specify `foo: Bar` instead of `foo: Union[Bar, Tuple[int, int]]`.
> 2. Do not use `from __future__ import annotations`, as that turns all types to be strings, which currently can't be inspected on class creation. 

# Planned
- ~~Added sync version of the client~~

# Not yet released
- nothing yet

# v0.0.17
-  🔨 Work around for `pydantic` version` 2` being a thing. We now specify it to use  `pydantic.v1` (if available, otherwise we assume it still is v1).

# v0.0.16
- 🔨 Fixed a lot more stuff to make it now work quite nicely with FastAPI.
- 🆕 Added a new `consume_from_row(row)` to make custom query result parsing easier.
- 🔨 Fixed it not handling timezones in `datetime`s but instead breaking.
    - It now converts everything it encounters to UTC first.
    - For naïve `datetime` objects, that is assumed to take the computer's local timezone and is adapted for that to UTC as well. 
- 🆕 Take into account when you set `Field(default=123)` or `Field(default_factory=datetime.utcnow)` as value of the class field.
- 🔨 Fixed `In[value, None]`.
- 🔨 Fixed `field=None` in `select(…)` and `get(…)` not properly resulting in `field IS NULL`.

# v0.0.15
- 🔨 Some fixes making it work with FastAPI.

# v0.0.14
- 🔨 Fixed `REFERENCES` for more than one level of primary key references. 

# v0.0.13
- 🔄 Made sure that the `async insert(…)` method contains no database-agnostic code, and that is actually refactored out to calls to new `_insert_preparation` and `_insert_postprocess` methods.
    - This is in preparation of the sync version of the client.  
- 🆕 Make classes referencing themself possible.
- 🔨 Fixed `DELETE` not working for primary keys which are references.
- 🔨 Fixed `REFERENCES` to other tables with a double Primary key now set the `FOREIGN KEY` correctly in one, as opposed to trying to create `FOREIGN KEY` for each field.
    - The old generated SQL would result in `ERROR: there is no unique constraint matching given keys for referenced table "<table name>"`

# v0.0.12
- 🆕 Added a new `FastORM.get_primary_keys_sql_fields()` method to get the sql column name(s) of the primary key(s).
- ⚠️ Replaced the `on_conflict_upsert_field_list` parameter of `FastORM.insert(…)` and `FastORM.build_sql_insert(…)` with a new `upsert_on_conflict`.
   - It is mostly the same, but the variable name speaks better what it does. That is, specifying the fields which it would conflict on.
   - Migration:
     - `on_conflict_upsert_field_list=['some_field', 'another_field']` is now `upsert_on_conflict=['some_field', 'another_field']`.
     - `on_conflict_upsert_field_list=None` is now `upsert_on_conflict=False`.
   - 🆕 Also there's a new case now:
     - `upsert_on_conflict=True`. Here it will now automatically use the primary keys to conflict on those.
- 🔨 Fixed `FastORM.insert(…)` and `FastORM.build_sql_insert(…)` that a key listed in the conflict list for UPSERTs would not be excluded from the UPDATE part of the UPSERT, the value overwriting part.
    

# v0.0.11
- 🔨 Fixed equality checking on deduplication if only one of them was a `ForwardRef`.
- 🔨 Fixed upgrading of `Any` failing.


# v0.0.10
- 🔨 Fixed `SELECT` syntax for table references
- 🔨 Fixed `INSERT`, `UPDATE`, `SELECT`, `DELETE` for new field syntax (where a field referencing `OtherTable.id` would be called `other_table__id`).
- 🔨 Fixed `from_row(…)` if you used namespaces (those from `get_select_fields(namespace="…")`) 
- ✅ Improved test coverage to pretty much everything.


# v0.0.9
- 🆕 Added caching for `get_fields_typehints(…)` and `get_fields_references(…)`.
- 🔨 Fixed error if you used a `ForwardRef` to reference a model not yet defined.
- ⚠️ Extracted the referencing part from the create stable stuff (`create_table(…)` and `build_sql_create(…)`). That way they can be executed independently (`create_table_references(…)` and `build_sql_references(…)`), so you can first create all the tables and then add the references.

# v0.0.8
- 🆕 added `get_connection_pool(…)` to get a connection pool ready to use.
- ⚠️ renamed `get_connection(…)` to `create_connection(…)`. To fit the naming of `get_connection_pool(…)`.
- 🔨 Fixed invalid `SELECT` query if there were no parameters given to `.select(…)` (or the underlying `.build_sql_select(…)`).
    - Empty selectors will continue to be allowed for `.get(…)` as well, to keep that funny case possible, where there's only one row in a table and you wanna simply `SomeTable.get()` it. 

# v0.0.7
- 🔨 Fix for CREATE table statement not allowing for a prepared statement, and thus needing the escaping of values.
    - ℹ️ Needs psycopg2 installed for complex types (everything other than None, bool, int, and pure ascii strings)
- 🆕 Added support for `PRIMARY KEY`(`S`).
- 🆕 Added support for creating `REFERENCES` to other tables. 
     
# v0.0.6
- 🆕 Handle `Optional[Table, <Table Primary Key Type>]` for references.
    - Works for referencing other tables with a multi-field primary key, too. 
- Added lots of unit tests and also fix bugs found by this
    - 🆕 added support for multiple primary keys in most places now
    - 🔨 fixes for errors/wonkyness introduced by using pydantic's BaseModel.

# v0.0.5
- 🆕 Added `create_table(cls, conn: Connection, if_not_exists: bool = False)`.
- 🆕 Added boolean `if_not_exists: bool = False` to the  `build_sql_create(…)`


# v0.0.4
- 🆕 Added `has_changes()` and `get_changes()`. 


# v0.0.3
- 🆕 Added `FastORM.build_sql_create()` with works quite well for simple database types.
    - This is a beta version of creating tables, and will be changed, modified and improved in the future.
    - That means if you use it, you man need to do manual migrations for a later version.
    - Refer to this changelog and search for `build_sql_create` to find more.
- 🔨 Fixed uninstallable dependency    

    
