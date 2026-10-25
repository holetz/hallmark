-- Daily revenue rollup (synthetic fixture).
INSERT OVERWRITE demo_catalog.sales.daily_revenue
SELECT
  CAST(o.ordered_at AS DATE) AS revenue_day,
  SUM(demo_catalog.sales.orders.order_total) AS revenue_total
FROM demo_catalog.sales.orders AS o
WHERE o.order_total > 0
GROUP BY CAST(o.ordered_at AS DATE);
