"""The optional backend hook keeps prefix and application parameters separate."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pgtrigger import runtime


@pytest.fixture
def cursor():
    return SimpleNamespace(
        name=None,
        connection=SimpleNamespace(
            info=SimpleNamespace(transaction_status=0), get_transaction_status=lambda: 0
        ),
    )


@pytest.fixture(params=["ignore", "schema"])
def injection(request, monkeypatch):
    if request.param == "ignore":
        monkeypatch.setattr(runtime._ignore, "value", {"test_trigger"}, raising=False)
        return runtime._inject_pgtrigger_ignore, "pgtrigger.ignore", "{test_trigger}"
    monkeypatch.setattr(runtime._schema, "value", ["public", "$user"], raising=False)
    return runtime._inject_schema, "search_path", 'public, "$user"'


@pytest.mark.parametrize("as_bytes", [False, True])
@pytest.mark.parametrize("kind", ["tuple", "mapping", "none", "many"])
def test_handoff_preserves_sql_and_parameters(cursor, injection, as_bytes, kind):
    inject, setting, value = injection
    sql = (
        "UPDATE example SET value = %(value)s"
        if kind == "mapping"
        else "UPDATE example SET value = %s"
    )
    sql = sql.encode() if as_bytes else sql
    rows = []

    def lazy_rows():
        rows.append("consumed")
        yield (42,)

    params = {
        "tuple": (42,),
        "mapping": {"value": 42},
        "none": None,
        "many": lazy_rows(),
    }[kind]
    prepend = Mock()
    execute = Mock()
    context = {"cursor": cursor, "pg_prepend_sql": prepend}
    result = inject(execute, sql, params, kind == "many", context)
    prepend.assert_called_once_with(f"SELECT set_config('{setting}', %s, true)", (value,))
    execute.assert_called_once_with(sql, params, kind == "many", context)
    assert execute.call_args.args[0] is sql
    assert execute.call_args.args[1] is params
    assert rows == []
    if kind == "mapping":
        assert params == {"value": 42}
    assert result is execute.return_value
    result.nextset.assert_not_called()


@pytest.mark.parametrize("skip", ["concurrent", "named", "errored"])
def test_handoff_preserves_injection_exclusions(cursor, injection, skip):
    inject, _, _ = injection
    sql = "CREATE INDEX CONCURRENTLY ix ON example (value)" if skip == "concurrent" else "SELECT 1"
    if skip == "named":
        cursor.name = "named"
    if skip == "errored":
        cursor.connection.info.transaction_status = 3
        cursor.connection.get_transaction_status = lambda: 3
    prepend = Mock()
    execute = Mock()
    context = {"cursor": cursor, "pg_prepend_sql": prepend}
    assert inject(execute, sql, None, False, context) is execute.return_value
    prepend.assert_not_called()
    execute.assert_called_once_with(sql, None, False, context)
    execute.return_value.nextset.assert_not_called()


def test_empty_schema_does_not_prepend(cursor, monkeypatch):
    monkeypatch.setattr(runtime._schema, "value", [], raising=False)
    prepend = Mock()
    execute = Mock()
    context = {"cursor": cursor, "pg_prepend_sql": prepend}
    runtime._inject_schema(execute, "SELECT 1", None, False, context)
    prepend.assert_not_called()
    execute.assert_called_once_with("SELECT 1", None, False, context)
    execute.return_value.nextset.assert_not_called()


def test_without_hook_retains_combined_execution(cursor, injection):
    inject, setting, value = injection
    execute = Mock()
    execute.return_value.nextset.return_value = None
    context = {"cursor": cursor}
    inject(execute, "SELECT %s", (42,), False, context)
    execute.assert_called_once_with(
        f"SELECT set_config('{setting}', %s, true); SELECT %s", [value, 42], False, context
    )
    if runtime.utils.psycopg_maj_version == 3:
        execute.return_value.nextset.assert_called_once_with()


def test_hook_failure_stops_execution(cursor, injection):
    inject, _, _ = injection
    execute = Mock()
    prepend = Mock(side_effect=ValueError("rejected prefix"))
    with pytest.raises(ValueError, match="rejected prefix"):
        inject(execute, "SELECT 1", None, False, {"cursor": cursor, "pg_prepend_sql": prepend})
    execute.assert_not_called()


@pytest.fixture
def prefix_backend(monkeypatch):
    """A test backend using only public Django and psycopg execution APIs."""
    if runtime.utils.psycopg_maj_version != 3:
        pytest.skip("Server-side binding requires psycopg 3")
    import contextlib

    import psycopg
    from django.db import connection

    connection.ensure_connection()
    raw = connection.connection
    monkeypatch.setattr(raw, "cursor_factory", psycopg.Cursor)
    observed = []

    def collect(execute, sql, params, many, context):
        prefixes = []
        context["pg_prepend_sql"] = lambda query, values: prefixes.insert(0, (query, values))
        context["test_prefixes"] = prefixes
        return execute(sql, params, many, context)

    def dispatch(execute, sql, params, many, context):
        prefixes = context["test_prefixes"]
        observed.append((prefixes.copy(), sql, params, many))
        with (
            connection.wrap_database_errors,
            raw.transaction(),
            contextlib.ExitStack() as cursors,
            raw.pipeline(),
        ):
            for prefix_sql, prefix_params in prefixes:
                cursors.enter_context(raw.cursor()).execute(prefix_sql, prefix_params)
            return execute(sql, params, many, context)

    return collect, dispatch, observed


@pytest.mark.django_db(transaction=True)
def test_public_wrappers_with_server_binding(prefix_backend):
    from django.contrib.contenttypes.models import ContentType
    from django.db import IntegrityError, connection

    import pgtrigger

    collect, dispatch, observed = prefix_backend
    trigger = pgtrigger.Protect(name="handoff_proof", operation=pgtrigger.Update)
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE handoff_trigger (value int PRIMARY KEY, ignored text, path text)"
        )
        try:
            sql = (
                "INSERT INTO handoff_trigger VALUES (%(value)s, "
                "current_setting('pgtrigger.ignore'), current_setting('search_path'))"
            )
            with (
                trigger.register(ContentType),
                connection.execute_wrapper(collect),
                pgtrigger.ignore(trigger.get_uri(ContentType)),
                pgtrigger.schema("public", "$user"),
                connection.execute_wrapper(dispatch),
            ):
                params = {"value": 1}
                cursor.execute(sql + " RETURNING value", params)
                assert cursor.fetchone() == (1,)
                assert cursor.nextset() is None
                assert params == {"value": 1}
                cursor.executemany(sql, ({"value": i} for i in (2, 3)))
                assert cursor.rowcount == 2
                with pytest.raises(IntegrityError):
                    cursor.executemany(sql, ({"value": i} for i in (4, 1)))
            for prefixes, *_ in observed:
                assert [sql for sql, _ in prefixes] == [
                    "SELECT set_config('search_path', %s, true)",
                    "SELECT set_config('pgtrigger.ignore', %s, true)",
                ]
            cursor.execute("SELECT * FROM handoff_trigger ORDER BY value")
            results = cursor.fetchall()
            assert [row[0] for row in results] == [1, 2, 3]
            assert all(trigger.get_pgid(ContentType) in row[1] for row in results)
            assert all(row[2] == 'public, "$user"' for row in results)
            cursor.execute("SELECT NULLIF(current_setting('pgtrigger.ignore', true), '')")
            assert cursor.fetchone() == (None,)
        finally:
            cursor.execute("DROP TABLE handoff_trigger")


@pytest.mark.django_db
@pytest.mark.parametrize("composed", [False, True])
def test_handoff_preserves_sql_objects(injection, composed):
    from django.db import connection

    inject, _, _ = injection
    sql = runtime.psycopg_sql.SQL("SELECT %s")
    if composed:
        sql = runtime.psycopg_sql.Composed([sql])
    execute = Mock()
    prepend = Mock()
    params = (42,)
    with connection.cursor() as cursor:
        context = {"cursor": cursor, "pg_prepend_sql": prepend}
        inject(execute, sql, params, False, context)
    assert execute.call_args.args[0] is sql
    assert execute.call_args.args[1] is params
    prepend.assert_called_once()
