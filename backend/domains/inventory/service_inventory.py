"""Business logic of the inventory domain: locations, stock and movements."""

from datetime import date

from loguru import logger
from neo4j import AsyncSession

from core.exceptions import BusinessLogicError, NotFoundError
from core.websocket import manager

from .repository_inventory import (
    LocationRepository,
    StockMovementRepository,
    StockRepository,
)
from .schemas_inventory import (
    Location,
    MovementType,
    Stock,
    StockMovement,
    StockMovementCreate,
    StockMovementResponse,
)


class LocationService:
    """Drives the business logic for locations.

    Pure pass-through: the endpoint returns a complete list without filters, and an empty
    list is a valid result — there is no rule to enforce here.
    """

    @staticmethod
    async def get_locations(session: AsyncSession) -> list[Location]:
        """Fetches every location.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[Location]: Every location, or an empty list.
        """
        return await LocationRepository.get_locations(session)


class StockService:
    """Drives the business logic for stock queries."""

    @staticmethod
    async def get_stock(product_number: str, session: AsyncSession) -> Stock:
        """Determines a product's stock across every location.

        Translates the repository's `None` into a business error. A product that exists but
        was never stored is not affected: it returns a stock of zeros with an empty
        `byLocation`, not a 404.

        Args:
            product_number (str): The product number.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Stock: Total stock, reservation and the breakdown per location.

        Raises:
            NotFoundError: When no product with that number exists.
        """
        stock = await StockRepository.get_stock(product_number, session)
        if stock is None:
            raise NotFoundError(f"A product with the number '{product_number}' does not exist.")
        return stock

    @staticmethod
    async def get_stock_list(
        session: AsyncSession, below_min_stock: bool = False
    ) -> list[Stock]:
        """Determines the stock of every product, optionally only those below their minimum.

        Pure pass-through: an empty list is a valid result, not an error — unlike the
        single lookup there is no product number here that could fail.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.
            below_min_stock (bool): Restricts to products with a maintained `minStock`
                whose total stock falls below it.

        Returns:
            list[Stock]: The stock of all (filtered) products.
        """
        return await StockRepository.get_stock_list(session, below_min_stock)


class StockMovementService:
    """Drives the business logic for bookings and the movement history."""

    @staticmethod
    async def post_movement(
        data: StockMovementCreate, session: AsyncSession
    ) -> StockMovementResponse:
        """Books a stock movement and advances the stock cache.

        Enforces the sign rule before the booking reaches the database: the direction sits
        in the type, so the amount is positive. The one exception is `Correction`, where
        the sign carries the information — stocktaking can go up as well as down. A
        correction of 0 is still rejected: it would be an entry in the audit log that says
        nothing.

        The stock check is deliberately NOT here. It needs the stored stock and has to run
        in the same transaction as the booking, otherwise a concurrent request can slip
        between check and write. The `BusinessLogicError` the repository raises for it is
        not caught here — it belongs at the global handler unchanged.

        Args:
            data (StockMovementCreate): The movement to book.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            StockMovementResponse: Stock and reservation after the booking, plus the
                server-assigned movement id.

        Raises:
            BusinessLogicError: When the quantity does not fit the movement type, or when
                the stock would go negative.
            NotFoundError: When product, location, customer or document do not exist.
        """
        quantity = data.quantity
        movement_type = data.type

        if movement_type == "Correction":
            if quantity == 0:
                raise BusinessLogicError(
                    "quantity must not be 0 on a correction: a booking without an effect "
                    "would be an audit log entry that says nothing."
                )
        elif quantity <= 0:
            raise BusinessLogicError(
                f"quantity has to be greater than 0 on '{movement_type}', but was "
                f"{quantity}. The direction follows from the movement type, not from the "
                "sign — only a correction may be negative."
            )

        # A path of its own instead of a branch in the repository: a transfer needs two
        # bookings with opposite signs and returns two movement ids.
        if movement_type == "Transfer":
            result = await StockMovementRepository.post_transfer(data, session)
        else:
            result = await StockMovementRepository.post_movement(data, session)

        logger.bind(
            movement_id=result.movementId,
            product_number=data.productNumber,
            type=movement_type,
            quantity=quantity,
        ).info("Stock movement booked.")

        await manager.send_event({
            "type": "event", "entity": "stock", "trigger": "stock_movement",
            "reference": data.documentNumber,
            "ids": [data.productNumber],
            "scope": "list",
        })
        return result

    @staticmethod
    async def get_movements(
        session: AsyncSession,
        product_number: str | None = None,
        location_id: str | None = None,
        type: MovementType | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[StockMovement]:
        """Fetches the movement history, optionally filtered.

        The filters are purely additive — the more are set, the narrower the result. An
        empty result list is a valid answer, not an error.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.
            product_number (str | None): Only movements of this product.
            location_id (str | None): Only movements in or out of this location.
            type (MovementType | None): Only movements of this type.
            from_date (date | None): Lower bound of the booking period, inclusive.
            to_date (date | None): Upper bound of the booking period, inclusive.

        Returns:
            list[StockMovement]: The movements found, newest first.
        """
        return await StockMovementRepository.get_movements(
            session,
            product_number=product_number,
            location_id=location_id,
            type=type,
            from_date=from_date,
            to_date=to_date,
        )
