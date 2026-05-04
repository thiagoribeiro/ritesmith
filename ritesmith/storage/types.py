"""Custom SQLAlchemy TypeDecorators que funcionam tanto em PostgreSQL quanto em SQLite.

Em PostgreSQL: usa ARRAY(Text), JSONB e TSVECTOR nativos.
Em SQLite (testes): usa JSON e Text como fallback.
"""
from sqlalchemy import JSON, Text, TypeDecorator
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.dialects.postgresql import TSVECTOR as PG_TSVECTOR


class JSONB(TypeDecorator):
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_JSONB())
        return dialect.type_descriptor(JSON())


class TextArray(TypeDecorator):
    """Lista de strings: ARRAY(Text) em PG, JSON em SQLite."""
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_ARRAY(Text()))
        return dialect.type_descriptor(JSON())


class TSVector(TypeDecorator):
    """TSVECTOR em PG (preenchido por trigger), Text em SQLite (sem FTS)."""
    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_TSVECTOR())
        return dialect.type_descriptor(Text())
