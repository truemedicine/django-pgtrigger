# Structured SQL prefix handoff

A custom database backend can opt into a structured handoff by placing a callable
at `context["pg_prepend_sql"]` in Django's `connection.execute_wrapper()` context,
before this library's wrappers run. This is a protocol shared by the Truemed
forks of django-pghistory and django-pgtrigger, not a built-in Django option.

```python
def prepend_sql(sql: str, params: tuple) -> None:
    prefixes.insert(0, (sql, params))
```

The callback registers one parameterized prefix statement, without a trailing
semicolon. Each statement has its own positional parameters. The library then
calls the next execution wrapper with the original application SQL, parameters,
`many` flag, and context. Named parameters are not changed, and `executemany()`
iterables are not consumed. The library continues to own context lifetime,
serialization, and the existing rules for when a prefix is appropriate.

The backend implementing the callback must:

- Scope the collected prefixes to a single execution, including cleanup on errors.
- Prepend new registrations ahead of earlier ones. This preserves the order of
  nested wrappers that previously prepended SQL strings.
- Execute the prefixes and application statement on the same connection, in the
  same transaction. In autocommit, introduce a transaction spanning the entire
  operation; executing each prefix separately in autocommit would lose its local
  settings before the application statement runs. On errors, preserve atomic
  rollback of the group, including a savepoint when needed in an existing transaction.
- For `executemany()`, keep the row iterable intact and execute prefixes once for
  the operation, with all rows in the same transaction.
- Translate driver errors into Django database exceptions, including errors
  deferred until a pipeline is synchronized or closed.
- Expose only the application operation's results and row count. With the hook
  enabled, these library wrappers do not call `nextset()` or drain results.
- Pass each SQL/parameter pair separately to the driver. The hook does not parse
  SQL or adapt parameter values, and does not require psycopg private methods.

Merely collecting prefixes without an executor is not a complete implementation.
A backend can install an outer collection wrapper and an inner dispatch wrapper
using Django's public `connection.execute_wrapper()` API. The inner wrapper runs
after the libraries have registered their prefixes. Transaction and pipeline
handling belong to the backend, not to the callback in this library.

Without the hook (or when it is `None`), existing concatenated SQL and result
handling are unchanged. This lets ordinary connections, including direct
migration connections, retain their existing behavior. The hook itself does not
enable server-side binding, configure a connection pool, or support arbitrary raw
multi-statement SQL.
