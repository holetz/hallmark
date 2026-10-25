-- Builds demo_catalog.sales.daily_revenue from demo_catalog.sales.orders
INSERT OVERWRITE demo_catalog.sales.daily_revenue
SELECT CAST(o.ordered_at AS DATE) AS revenue_day, SUM(o.order_total) AS revenue_total
FROM demo_catalog.sales.orders o
GROUP BY 1;
