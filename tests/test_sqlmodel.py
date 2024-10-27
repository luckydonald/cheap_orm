import unittest
from datetime import datetime
from typing import Optional, Union, Any, Type, List, Tuple, Dict, ForwardRef

# from fastorm import FastORM, Autoincrement, FieldInfo
# from fastorm.compat import get_type_hints_with_annotations
from tests.tools_for_the_tests_of_fastorm import extract_create_and_reference_sql_from_docstring, VerboseTestCase


from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine, select


class Hero(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    secret_name: str
    age: Optional[int] = None


engine = create_engine("sqlite:///database.db")

SQLModel.metadata.create_all(engine)


# noinspection DuplicatedCode
class CreateTableTestCase(VerboseTestCase):


    def test_example_code(self):
        hero_1 = Hero(name="Deadpond", secret_name="Dive Wilson")
        hero_2 = Hero(name="Spider-Boy", secret_name="Pedro Parqueador")
        hero_3 = Hero(name="Rusty-Man", secret_name="Tommy Sharp", age=48)

        with Session(engine) as session:
            session.add(hero_1)
            session.add(hero_2)
            session.add(hero_3)
            session.commit()

            statement = select(Hero).where(Hero.name == "Spider-Boy")
            hero = session.exec(statement).first()
            print(hero)
    # end def
# end class


if __name__ == '__main__':
    unittest.main()
# end if
