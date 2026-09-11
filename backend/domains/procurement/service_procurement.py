"""Business logic of the procurement domain: suppliers, supply ranges and reorder suggestions."""

from loguru import logger
from neo4j import AsyncSession

from core.exceptions import NotFoundError
from core.websocket import manager

from .repository_procurement import ReorderSuggestionRepository, SupplierRepository
from .schemas_procurement import (
    ReorderSuggestion,
    Supplier,
    SupplierCreate,
    SupplierProductCreate,
    SupplierUpdate,
)


class SupplierService:
    """Drives the business logic for supplier master data and supply ranges.

    Acts as the link between the API router and the repository. This layer's own
    contribution is narrow and sits in one place: it translates the repository's `None`
    into a business error. The data access layer stays free of HTTP semantics that way —
    whether a missing supplier is an error is not decided by the query.
    """

    @staticmethod
    async def get_supplier(id: str, session: AsyncSession) -> Supplier:
        """Looks up a supplier by its id in the Neo4j graph.

        Args:
            id (str): The unique id of the supplier.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Supplier: The supplier data that was found.

        Raises:
            NotFoundError: When no supplier with that id exists.
        """
        supplier = await SupplierRepository.get_supplier(id, session)
        if supplier is None:
            raise NotFoundError(f"A supplier with the id '{id}' does not exist.")
        return supplier

    @staticmethod
    async def get_suppliers(session: AsyncSession, search: str | None = None) -> list[Supplier]:
        """Fetches the list of suppliers, optionally filtered by a free text.

        An empty result list is a valid answer and not an error — a search term without
        hits is the normal case and nothing the client would have to handle through the
        error path.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.
            search (str | None): Free text across name and city, unfiltered without a
                value.

        Returns:
            list[Supplier]: The suppliers found, or an empty list.
        """
        return await SupplierRepository.get_suppliers(session, search=search)

    @staticmethod
    async def create_supplier(supplier_data: SupplierCreate, session: AsyncSession) -> Supplier:
        """Creates a new supplier in the graph.

        Args:
            supplier_data (SupplierCreate): The master data of the new supplier.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Supplier: The freshly created supplier including the server-assigned id and
                the creation timestamp.
        """
        supplier = await SupplierRepository.create_supplier(supplier_data, session)
        logger.bind(supplier_id=supplier.id).info("Supplier created successfully.")

        await manager.send_event({
            "type": "event", "entity": "supplier", "trigger": "supplier_created",
            "reference": supplier.id, "ids": [supplier.id], "scope": "list",
        })
        return supplier

    @staticmethod
    async def update_supplier(
        id: str, supplier_data: SupplierUpdate, session: AsyncSession
    ) -> Supplier:
        """Updates individual fields of an existing supplier.

        Passes the write model on unchanged instead of sorting out fields itself: which
        fields the client actually sent is known only to the model. Were this layer to
        pre-filter, the repository could no longer tell a deliberately sent `null` from a
        field that was not sent at all.

        Args:
            id (str): The unique id of the supplier.
            supplier_data (SupplierUpdate): The fields to change.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Supplier: The updated supplier.

        Raises:
            NotFoundError: When no supplier with that id exists.
            BusinessLogicError: When not a single field was handed over to change.
        """
        supplier = await SupplierRepository.update_supplier(id, supplier_data, session)
        if supplier is None:
            raise NotFoundError(f"A supplier with the id '{id}' does not exist.")
        logger.bind(supplier_id=supplier.id).info("Supplier updated successfully.")

        await manager.send_event({
            "type": "event", "entity": "supplier", "trigger": "supplier_updated",
            "reference": supplier.id, "ids": [supplier.id], "scope": "list",
        })
        return supplier

    @staticmethod
    async def add_supplied_product(
        id: str, product_data: SupplierProductCreate, session: AsyncSession
    ) -> dict:
        """Takes a product into the supply range of a supplier.

        Deliberately returns a dictionary and not a response schema: it additionally
        carries the flag `wasUpdated`, from which the router picks 201 or 200. The
        endpoint's `response_model` then filters the flag back out of the answer to the
        client.

        Args:
            id (str): The unique id of the supplier.
            product_data (SupplierProductCreate): Product number and conditions.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            dict: The condition that was set, extended by `wasUpdated`.

        Raises:
            NotFoundError: When the supplier or the product does not exist.
        """
        result = await SupplierRepository.add_supplied_product(id, product_data, session)

        await manager.send_event({
            "type": "event", "entity": "supplier", "trigger": "supply_range_changed",
            "reference": id, "ids": [id], "scope": "list",
        })
        return result


class ReorderSuggestionService:
    """Drives the business logic of the weekly reorder analysis.

    Pure pass-through: the four selection rules — exclusion of replaced products, the
    mandatory filter on the minimum stock, the stock determination even without a stock
    record, and the ordering of the suppliers — are conditions of the Cypher query and
    cannot sensibly be layered on top of it. The two arithmetic rules sit in the
    repository's conversion, because only there is the raw result with `targetStock` and
    `totalStock` available.
    """

    @staticmethod
    async def get_reorder_suggestions(session: AsyncSession) -> list[ReorderSuggestion]:
        """Determines every product whose stock has fallen below its minimum stock.

        An empty list is a valid result: no product below its minimum stock is the best
        news the analysis can deliver, and no error case.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[ReorderSuggestion]: The suggestions with quantity and source of supply,
                largest quantity first, or an empty list.
        """
        return await ReorderSuggestionRepository.get_reorder_suggestions(session)
