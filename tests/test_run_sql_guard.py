"""run_sql takes SQL written by the model, which reads a question from the public: it must only ever read the
prepared tables, briefly, and return a bounded result."""
import pytest

import engine


def ok(sql):
    out = engine.run_sql(sql)
    assert "error" not in out, out
    return out


def refused(sql):
    out = engine.run_sql(sql)
    assert "error" in out, f"not refused: {sql!r} -> {out}"
    return out["error"]


def test_legitimate_queries():
    assert ok("SELECT zone_name, rent_zori FROM zone_features ORDER BY rent_zori")["row_count"] == 2
    assert ok("WITH t AS (SELECT * FROM zone_features) SELECT count(*) AS n FROM t")["rows"] == [[2]]
    assert ok("select f.zone_name from zone_features f join zones z using (zone_id) where z.borough = 'Queens';")["rows"] == [["Astoria"]]
    ok("DESCRIBE zone_features")


@pytest.mark.parametrize("sql", [
    "INSERT INTO zones VALUES (3, 'x', 'y', 'z', NULL)",
    "DELETE FROM zones",
    "UPDATE zone_features SET rent_zori = 0",
    "DROP TABLE zones",
    "CREATE TABLE t AS SELECT 1",
    "SELECT 1; DROP TABLE zones",
    "SELECT * FROM zones; SELECT 1",
    "ATTACH '/tmp/x.db' AS x",
    "COPY zones TO '/tmp/out.csv'",
    "EXPORT DATABASE '/tmp/dump'",
    "INSTALL httpfs",
    "LOAD httpfs",
    "SET enable_external_access = true",
    "PRAGMA database_list",
    "CALL pragma_version()",
])
def test_writes_and_settings_refused(sql):
    refused(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_csv_auto('/etc/passwd')",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM read_text('/proc/self/environ')",
    "SELECT * FROM read_blob('/app/.env')",
    "SELECT * FROM read_json_auto('http://169.254.169.254/latest/meta-data/')",
    "SELECT * FROM read_parquet('https://example.com/x.parquet')",
    "SELECT * FROM parquet_scan('/app/data/x.parquet')",
    "SELECT * FROM '/etc/passwd'",
    "SELECT * FROM 'data/zones.geojson'",
    "FROM 'https://example.com/a.csv'",
    "SELECT * FROM glob('/home/*')",
    "SELECT * FROM st_read('/etc/hosts')",
    "SELECT getenv('ANTHROPIC_API_KEY')",
    "SELECT current_setting('home_directory')",
    "SELECT * FROM duckdb_settings()",
    "SELECT * FROM duckdb_secrets()",
    "SELECT * FROM query('SELECT 1')",
    "SELECT * FROM sqlite_scan('/x.db', 't')",
])
def test_files_network_and_environment_refused(sql):
    refused(sql)


def test_connection_blocks_file_access_even_without_the_filter():
    # second layer: enable_external_access=false on the connection itself, locked
    for sql in ("SELECT * FROM read_csv_auto('/etc/passwd')", "SELECT * FROM read_text('/etc/hostname')",
                "SET enable_external_access = true"):
        with pytest.raises(Exception):
            engine.q(sql)


def test_environment_not_readable_through_sql(monkeypatch):
    monkeypatch.setenv("WNYC_TEST_SECRET", "s3cret")
    try:
        rows = engine.q("SELECT getenv('WNYC_TEST_SECRET') AS v")
    except Exception:
        return  # refused by the connection: fine
    assert rows[0]["v"] != "s3cret"


@pytest.mark.parametrize("sql", [
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM sqlite_master",
    "SELECT * FROM not_a_table",
])
def test_only_prepared_tables(sql):
    refused(sql)


def test_slow_query_is_interrupted(monkeypatch):
    monkeypatch.setattr(engine, "SQL_TIMEOUT_S", 0.5)
    err = refused("SELECT count(*) FROM stations a, stations b, stations c, stations d, stations e")
    assert "stopped after" in err


def test_result_is_bounded():
    out = ok("SELECT stop_name, repeat('x', 100000) AS big FROM stations")
    assert out["row_count"] <= 50 and out["truncated"]
    assert all(len(r[1]) <= engine.MAX_CELL_CHARS + 1 for r in out["rows"])
    import json
    assert len(json.dumps(out)) < engine.MAX_RESULT_CHARS + 2000


def test_query_length_capped():
    assert "too long" in refused("SELECT 1 AS a" + " , 1" * 3000 + " FROM zones")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM generate_series(1, 3)",
    "SELECT count(*) FROM range(10)",
    "SELECT rent_zori, service_zone FROM zone_features JOIN zones USING (zone_id)",
    "WITH a AS (SELECT * FROM zones), b AS (SELECT * FROM a) SELECT count(*) FROM b",
    "FROM zone_features SELECT zone_name",
])
def test_safe_shapes_allowed(sql):
    ok(sql)


@pytest.mark.parametrize("sql", [
    "SELECT n FROM zone_features WHERE zone_id IN (SELECT 1 FROM main.secrets)",
    "WITH x AS (SELECT * FROM 'file.csv') SELECT * FROM x",
    "SELECT * FROM json_each('[1]')",
    "SELECT (SELECT max(zone_id) FROM zones) AS a, * FROM sniff_csv('/etc/passwd')",
])
def test_tree_level_checks(sql):
    refused(sql)
