"""Revenue and margin reports over invoice lines."""

from datetime import date
from decimal import Decimal

from neo4j import AsyncSession
from neo4j.exceptions import Neo4jError

from core.exceptions import (
    DatabaseError,
)
from core.neo4j_query import read_many

from ..schemas_sales import (
    RevenueGroupBy,
    RevenueLine,
)
from ._shared import _euro

# Delivers one row per invoice line in the period, with the order discount already applied
# proportionally and the cost price of the product beside it. The grouping itself
# (customer/product/month) happens afterwards in Python — that way both groupings are
# guaranteed to sum exactly the same rows and their totals can never differ.
_REVENUE_LINES_QUERY = """
MATCH (d:Invoice)-[:HAS_LINE]->(l:DocumentLine)-[:OF_PRODUCT]->(p:Product)
WHERE d.date >= $fromDate AND d.date <= $toDate
OPTIONAL MATCH (d)-[:BELONGS_TO_CUSTOMER]->(c:Customer)
WITH d, c,
     collect({
         productNumber: p.number,
         label:         p.label,
         costPriceCent: p.costPriceCent,
         quantity:      coalesce(l.quantity, 0.0),
         hasFixedPrice: coalesce(l.hasFixedPrice, false),
         amountCent:    round(coalesce(l.quantity, 0.0) * coalesce(l.unitPriceCent, 0)
                               * (1 - coalesce(l.discountPercent, 0.0) / 100.0))
     }) AS rows
WITH d, c, rows
UNWIND rows AS row
// The order discount hangs off the document, not off the line — every line without a
// fixed price carries its own share (row.amountCent * rate), a fixed-price line stays
// undiscounted. That is no approximation: for a line without a fixed price, "own share"
// and "the total discount distributed proportionally over the subtotal" are the same value.
WITH d, c, row,
     CASE WHEN row.hasFixedPrice THEN 0.0
          ELSE row.amountCent * coalesce(d.orderDiscountPercent, 0.0) / 100.0
     END AS proportionalDiscountCent
RETURN c.name AS customerName,
       row.productNumber AS productNumber,
       row.label AS label,
       row.quantity AS quantity,
       d.date AS date,
       toInteger(round(row.amountCent - proportionalDiscountCent)) AS revenueCent,
       CASE WHEN row.costPriceCent IS NULL THEN null
            ELSE toInteger(round(row.quantity * row.costPriceCent)) END AS costCent
"""


class ReportRepository:
    """Aggregates revenue and margin over document lines."""

    @staticmethod
    def _group_key(row: dict, group_by: RevenueGroupBy) -> str:
        """Determines the grouping key of a row.

        A missing customer name does not occur in practice — every invoice is a sales
        document and carries exactly one customer — but stays as a fallback so a row
        imported with gaps does not group under `None`.
        """
        if group_by == "customer":
            return row["customerName"] or "Unknown"
        if group_by == "product":
            return row["label"] or row["productNumber"]
        date_value = row["date"]
        return f"{date_value.year:04d}-{date_value.month:02d}"

    @staticmethod
    async def revenue(
        from_date: date, to_date: date, group_by: RevenueGroupBy, session: AsyncSession
    ) -> list[RevenueLine]:
        """Sums revenue and margin over invoices in the period.

        Summed are exclusively `Invoice` documents — delivery notes do not count, otherwise
        the same operation would be recorded twice.

        If a line of the group has no determinable cost price, the margin of the whole
        group is `None`, not the remainder shortened by the missing line: an incomplete
        margin would not be a mistake anyone would notice.

        Args:
            from_date (date): Lower bound of the document date, inclusive.
            to_date (date): Upper bound of the document date, inclusive.
            group_by (RevenueGroupBy): Grouping by customer, product or month.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[RevenueLine]: Revenue, cost price and margin per group, sorted by group
                name.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        try:
            rows = await read_many(
                session, _REVENUE_LINES_QUERY, fromDate=from_date, toDate=to_date
            )
        except Neo4jError as e:
            raise DatabaseError(f"Database error during the revenue report: {e}") from e

        groups: dict[str, list[dict]] = {}
        for row in rows:
            key = ReportRepository._group_key(row, group_by)
            groups.setdefault(key, []).append(row)

        result: list[RevenueLine] = []
        for key, lines in sorted(groups.items()):
            revenue_cent = sum(line["revenueCent"] for line in lines)
            cost_known = all(line["costCent"] is not None for line in lines)
            total_cost_cent = sum(line["costCent"] for line in lines) if cost_known else None
            margin_cent = (
                revenue_cent - total_cost_cent
                if cost_known and total_cost_cent is not None
                else None
            )

            margin_percent = None
            if margin_cent is not None and revenue_cent:
                margin_percent = round(margin_cent / revenue_cent * 100, 2)

            result.append(
                RevenueLine(
                    group=key,
                    revenue=_euro(revenue_cent) or Decimal("0.00"),
                    totalCost=_euro(total_cost_cent),
                    margin=_euro(margin_cent),
                    marginPercent=margin_percent,
                )
            )
        return result
