"""Nightly job that segments the rows of demo_catalog.crm.customers."""


def keep_gold(rows):
    return [r for r in rows if r["segment"] == "gold"]


def label(row):
    return row["full_name"].title()
