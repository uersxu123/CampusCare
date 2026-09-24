from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.dialects.mysql.base import MySQLDDLCompiler
from sqlalchemy.sql import functions, sqltypes

from app.core.config import get_settings
from app.core.database import Base
from app.models import entities  # noqa: F401


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata


class MigrationMySQLDDLCompiler(MySQLDDLCompiler):
    def get_column_default_string(self, column):
        default = super().get_column_default_string(column)
        type_impl = column.type._unwrapped_dialect_impl(self.dialect)
        if (
            default is not None
            and self.dialect.is_mariadb is not True
            and (self.dialect.server_version_info or ()) >= (8, 0, 13)
            and isinstance(type_impl, (sqltypes.Text, sqltypes.LargeBinary, sqltypes.JSON))
            and not isinstance(column.server_default.arg, functions.FunctionElement)
        ):
            return f"({default})"
        return default


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        if connection.dialect.name == "mysql":
            connection.dialect.ddl_compiler = MigrationMySQLDDLCompiler
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_offline() if context.is_offline_mode() else run_migrations_online()
