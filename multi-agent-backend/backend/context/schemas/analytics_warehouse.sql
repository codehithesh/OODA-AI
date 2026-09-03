-- Analytics warehouse schema (DuckDB). This DDL is injected into the
-- generate_sql prompt AND applied to the embedded DuckDB file at startup.
CREATE TABLE IF NOT EXISTS orders (
  order_id INTEGER,
  customer_id INTEGER,
  region VARCHAR,
  status VARCHAR,
  amount DECIMAL(12, 2),
  ordered_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_metrics (
  day DATE,
  metric VARCHAR,
  value DOUBLE
);
