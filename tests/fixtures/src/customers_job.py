"""Segment job for the synthetic customers table (fixture)."""


def build(spark):
    customers = spark.table("demo_catalog.crm.customers")
    named = customers.select("customer_id", "full_name", "segment")
    return named.filter(named.segment == "retail")


def unrelated():
    # Mentions a column-like word in a file that never names a table of ours.
    return {"note": "no tables here"}
