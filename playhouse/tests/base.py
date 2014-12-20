import logging
import os
import sys
from contextlib import contextmanager
from unittest import TestCase

from peewee import *
from peewee import AliasMap
from peewee import logger
from peewee import print_
from peewee import QueryCompiler
from peewee import SelectQuery

# Register psycopg2 compatibility hooks.
try:
    from pyscopg2cffi import compat
    compat.register()
except ImportError:
    pass
else:
    try:
        import psycopg2
    except ImportError:
        pass

# Python 2/3 compatibility.
if sys.version_info[0] < 3:
    import codecs
    ulit = lambda s: codecs.unicode_escape_decode(s)[0]
    binary_construct = buffer
    binary_types = buffer
else:
    ulit = lambda s: s
    binary_construct = lambda s: bytes(s.encode('raw_unicode_escape'))
    binary_types = (bytes, memoryview)


TEST_BACKEND = os.environ.get('PEEWEE_TEST_BACKEND') or 'sqlite'
TEST_DATABASE = os.environ.get('PEEWEE_TEST_DATABASE') or 'peewee_test'
TEST_VERBOSITY = int(os.environ.get('PEEWEE_TEST_VERBOSITY') or 1)

if TEST_VERBOSITY > 1:
    handler = logging.StreamHandler()
    handler.setLevel(logging.ERROR)
    logger.addHandler(handler)


class DatabaseInitializer(object):
    def __init__(self, backend, database_name):
        self.backend = self.normalize(backend)
        self.database_name = database_name

    def normalize(self, backend):
        backend = backend.lower().strip()
        mapping = {
            'postgres': ('postgresql', 'pg', 'psycopg2'),
            'sqlite': ('sqlite3', 'pysqlite'),
            'berkeleydb': ('bdb', 'berkeley'),
        }
        for key, alias_list in mapping.items():
            for db_alias in alias_list:
                if backend == db_alias:
                    return key
        return backend

    def get_database_class(self):
        mapping = {
            'postgres': PostgresqlDatabase,
            'sqlite': SqliteDatabase,
            'mysql': MySQLDatabase,
        }
        try:
            from playhouse.apsw_ext import APSWDatabase
        except ImportError:
            pass
        else:
            mapping['apsw'] = APSWDatabase

        try:
            from playhouse.berkeleydb import BerkeleyDatabase
        except ImportError:
            pass
        else:
            mapping['berkeleydb'] = BerkeleyDatabase

        try:
            from playhouse.sqlcipher_ext import SqlCipherDatabase
        except ImportError:
            pass
        else:
            mapping['sqlcipher'] = SqlCipherDatabase

        try:
            return mapping[self.backend]
        except KeyError:
            print_('Unrecognized database: "%s".' % self.backend)
            print_('Available choices:\n%s' % '\n'.join(
                sorted(mapping.keys())))
            raise

    def get_database(self):
        method = 'get_%s_database' % self.backend
        db_class = self.get_database_class()
        if not hasattr(self, method):
            return db_class(self.database_name)
        else:
            return getattr(self, method)(db_class)

    def get_apsw_database(self, db_class):
        return db_class('%s.db' % self.database_name, timeout=1000)

    def get_berkeleydb_database(self, db_class):
        return db_class('%s.bdb.db' % self.database_name, timeout=1000)

    def get_sqlcipher_database(self, db_class):
        return db_class(
            '%s.cipher.db' % self.database_name,
            passphrase='snakeoilpassphrase')

    def get_sqlite_database(self, db_class):
        return db_class('%s.db' % self.database_name)


class TestAliasMap(AliasMap):
    def add(self, obj, alias=None):
        if isinstance(obj, SelectQuery):
            self._alias_map[obj] = obj._alias
        else:
            self._alias_map[obj] = obj._meta.db_table


class TestQueryCompiler(QueryCompiler):
    alias_map_class = TestAliasMap


class TestDatabase(SqliteDatabase):
    compiler_class = TestQueryCompiler
    field_overrides = {}
    interpolation = '?'
    op_overrides = {}
    quote_char = '"'

    def sql_error_handler(self, exception, sql, params, require_commit):
        self.last_error = (sql, params)
        return super(TestDatabase, self).sql_error_handler(
            exception, sql, params, require_commit)


class QueryLogHandler(logging.Handler):
    def __init__(self, *args, **kwargs):
        self.queries = []
        logging.Handler.__init__(self, *args, **kwargs)

    def emit(self, record):
        self.queries.append(record)


database_initializer = DatabaseInitializer(TEST_BACKEND, TEST_DATABASE)

database_class = database_initializer.get_database_class()
test_db = database_initializer.get_database()
query_db = TestDatabase(':memory:')

compiler = query_db.compiler()
normal_compiler = QueryCompiler('"', '?', {}, {})


class TestModel(Model):
    class Meta:
        database = test_db


class PeeweeTestCase(TestCase):
    def setUp(self):
        self.qh = QueryLogHandler()
        logger.setLevel(logging.DEBUG)
        logger.addHandler(self.qh)

    def tearDown(self):
        logger.removeHandler(self.qh)

    def queries(self):
        return [x.msg for x in self.qh.queries]

    @contextmanager
    def assertQueryCount(self, num):
        qc = len(self.queries())
        yield
        self.assertEqual(len(self.queries()) - qc, num)

    def parse_node(self, query, expr_list, compiler=compiler):
        am = compiler.calculate_alias_map(query)
        return compiler.parse_node_list(expr_list, am)

    def parse_query(self, query, node, compiler=compiler):
        am = compiler.calculate_alias_map(query)
        if node is not None:
            return compiler.parse_node(node, am)
        return '', []

    def make_fn(fn_name, attr_name):
        def inner(self, query, expected, expected_params, compiler=compiler):
            fn = getattr(self, fn_name)
            att = getattr(query, attr_name)
            sql, params = fn(query, att, compiler=compiler)
            self.assertEqual(sql, expected)
            self.assertEqual(params, expected_params)
        return inner

    assertSelect = make_fn('parse_node', '_select')
    assertWhere = make_fn('parse_query', '_where')
    assertGroupBy = make_fn('parse_node', '_group_by')
    assertHaving = make_fn('parse_query', '_having')
    assertOrderBy = make_fn('parse_node', '_order_by')

    def assertJoins(self, sq, exp_joins, compiler=compiler):
        am = compiler.calculate_alias_map(sq)
        clauses = compiler.generate_joins(sq._joins, sq.model_class, am)
        joins = [compiler.parse_node(clause, am)[0] for clause in clauses]
        self.assertEqual(sorted(joins), sorted(exp_joins))

    def new_connection(self):
        return database_initializer.get_database()


class ModelTestCase(PeeweeTestCase):
    requires = None

    def setUp(self):
        super(ModelTestCase, self).setUp()
        if self.requires:
            test_db.drop_tables(self.requires, True)
            test_db.create_tables(self.requires)

    def tearDown(self):
        super(ModelTestCase, self).tearDown()
        if self.requires:
            test_db.drop_tables(self.requires, True)


def skip_if(expression):
    def decorator(klass):
        if expression():
            if TEST_VERBOSITY > 0:
                print_('Skipping %s tests.' % klass.__name__)
            class Dummy(object): pass
            return Dummy
        return klass
    return decorator
