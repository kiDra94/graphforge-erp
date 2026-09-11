"""Reads the raw data of the price calculation: base price and every discount source."""

from decimal import Decimal
from typing import NamedTuple

from neo4j import AsyncSession
from neo4j.exceptions import Neo4jError

from core.exceptions import (
    DatabaseError,
)
from core.neo4j_query import read_single

from ..schemas_sales import (
    PriceCalculationRequest,
)
from ._shared import _euro


class DiscountCandidate(NamedTuple):
    """A valid discount source of the line level, already checked against the date.

    `fixedPrice` and `percent` can both be set, as on a `CONDITION_FOR` edge: if
    `fixedPrice` is set it applies unchanged, and `percent` only describes the rate
    reported on the same condition — the effect on the price then comes exclusively from
    the fixed price.
    """
    type: str
    source: str
    fixedPrice: Decimal | None = None
    percent: float = 0.0


class PricingBasis(NamedTuple):
    """The raw data read for the price calculation, before the best price is resolved.

    Separates the reading (here) from the calculating (`_best_line_price` in the service) —
    the same pattern as `DocumentDetail` without the amounts the service fills in later.
    """
    productNumber: str
    basePrice: Decimal
    discountable: bool
    candidates: list[DiscountCandidate]


# Three independent sources, collected in separate CALL subqueries and brought together at
# the end. Kept apart instead of joined in one go, because a product can have no, one or
# several sources at the same time, and a shared MATCH would otherwise multiply them
# crosswise (a cartesian product of contract and group conditions).
_PRICING_BASIS_QUERY = """
OPTIONAL MATCH (c:Customer {id: $customerId})
OPTIONAL MATCH (p:Product {number: $productNumber})
WITH c, p
WHERE c IS NOT NULL AND p IS NOT NULL
CALL (c, p) {
    // The contract discount is flat and applies to every product of the contract, not
    // only to those with a condition of their own. If the product has a condition with a
    // fixed price, that beats the flat rate — so at most one candidate comes out of a
    // contract, never both at once.
    MATCH (ct:Contract)
    WHERE ct.validFrom <= $date AND ct.validTo >= $date
      AND (ct.isGlobal = true OR EXISTS { (c)-[:HAS_CONTRACT]->(ct) })
    OPTIONAL MATCH (ct)-[cf:CONDITION_FOR]->(p)
    WITH ct, cf
    WHERE (cf IS NOT NULL AND cf.fixedPriceCent IS NOT NULL) OR ct.discountPercent IS NOT NULL
    RETURN collect({
        type:           CASE WHEN cf IS NOT NULL AND cf.fixedPriceCent IS NOT NULL
                             THEN 'ContractFixedPrice' ELSE 'ContractDiscount' END,
        source:         ct.name + ' (contract ' + toString(ct.id) + ')',
        fixedPriceCent: CASE WHEN cf IS NOT NULL THEN cf.fixedPriceCent ELSE null END,
        percent:        CASE WHEN cf IS NOT NULL AND cf.fixedPriceCent IS NOT NULL
                             THEN 0.0 ELSE coalesce(ct.discountPercent, 0.0) END
    }) AS contractCandidates
}
CALL (p) {
    MATCH (d:Discount)-[:APPLIES_TO_PRODUCT]->(p)
    WHERE d.validFrom <= $date AND d.validTo >= $date
    RETURN collect({
        type: 'ProductDiscount', source: 'Discount ' + toString(d.id),
        fixedPriceCent: null, percent: d.value
    }) AS productCandidates
}
CALL (p) {
    OPTIONAL MATCH (p)-[:BELONGS_TO]->(:Subcategory)-[:PART_OF]->(g:ProductGroup)
    OPTIONAL MATCH (d:Discount)-[:APPLIES_TO_GROUP]->(g)
    WITH d, g
    WHERE d IS NOT NULL AND d.validFrom <= $date AND d.validTo >= $date
    RETURN collect({
        type: 'GroupDiscount',
        source: 'Discount ' + toString(d.id) + ' (product group ' + toString(g.id) + ')',
        fixedPriceCent: null, percent: d.value
    }) AS groupCandidates
}
RETURN p.number AS productNumber, p.listPriceCent AS listPriceCent,
       coalesce(p.discountable, true) AS discountable,
       contractCandidates + productCandidates + groupCandidates AS candidates
"""


class PriceCalculationRepository:
    """Reads the raw data the price calculation needs out of the graph."""

    @staticmethod
    async def read_pricing_basis(
        request: PriceCalculationRequest, session: AsyncSession
    ) -> PricingBasis | None:
        """Reads base price, discountability and every discount source valid on the date.

        Picking among the candidates (the best price) does not happen here — that is the
        job of `_best_line_price` in the service, which needs no database access and is
        therefore testable on its own.

        Args:
            request (PriceCalculationRequest): Customer, product and reference date.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            PricingBasis | None: The raw data, or None when customer or product do not
                exist.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        try:
            record = await read_single(
                session,
                _PRICING_BASIS_QUERY,
                customerId=request.customerId,
                productNumber=request.productNumber,
                date=request.date,
            )
            if record is None:
                return None

            candidates = [
                DiscountCandidate(
                    type=candidate["type"],
                    source=candidate["source"],
                    fixedPrice=_euro(candidate["fixedPriceCent"]),
                    percent=candidate["percent"],
                )
                for candidate in record["candidates"]
            ]
            # A product without a maintained sales price cannot be told apart from a
            # product with a maintained 0 EUR — both calculate as 0, instead of aborting
            # the calculation with a None.
            return PricingBasis(
                productNumber=record["productNumber"],
                basePrice=_euro(record["listPriceCent"]) or Decimal("0.00"),
                discountable=record["discountable"],
                candidates=candidates,
            )
        except Neo4jError as e:
            raise DatabaseError(f"Database error during the price calculation: {e}") from e
