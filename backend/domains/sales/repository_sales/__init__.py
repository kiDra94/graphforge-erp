"""Cypher queries of the sales domain, split by aggregate.

Sales is the largest domain in the system, and the document chain alone is larger than any
other domain's repository. One module per aggregate keeps each file readable; the pure rules
the document chain books by sit apart in `rules`, so they can be read and tested without the
Cypher around them.

Code outside the package imports from here. Only the unit tests reach into the modules, for
the private helpers they test directly.
"""

from .contracts import ContractRepository
from .customers import CustomerRepository
from .documents import DocumentRepository
from .pricing import DiscountCandidate, PriceCalculationRepository, PricingBasis
from .reports import ReportRepository

__all__ = [
    "ContractRepository",
    "CustomerRepository",
    "DiscountCandidate",
    "DocumentRepository",
    "PriceCalculationRepository",
    "PricingBasis",
    "ReportRepository",
]
