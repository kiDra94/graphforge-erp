"""Business logic of the catalog domain, between router and repository."""

from loguru import logger
from neo4j import AsyncSession

from core.exceptions import BusinessLogicError, NotFoundError
from core.websocket import manager

from .repository_catalog import BomRepository, CategoryRepository, ProductRepository
from .schemas_catalog import (
    BomLineCreate,
    BomLineDeleted,
    CategoryOption,
    Product,
    ProductCreate,
    ProductType,
    ProductUpdate,
)


class ProductService:
    """Drives the business logic for products.

    Acts as the link between the API router and the database repository, so the router
    stays free of logic and every database call stays encapsulated.
    """

    @staticmethod
    async def get_product(number: str, session: AsyncSession) -> Product:
        """Looks a product up by its number.

        Args:
            number (str): The unique product number.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Product: The product found.

        Raises:
            NotFoundError: When no product with that number exists.
        """
        product = await ProductRepository.get_product(number, session)
        if not product:
            raise NotFoundError(f"A product with the number '{number}' does not exist.")
        return product

    @staticmethod
    async def get_products(
        session: AsyncSession,
        search: str | None = None,
        type: ProductType | None = None,
        active: bool = True,
    ) -> list[Product]:
        """Fetches a filtered list of products.

        The filters are purely additive — the more are set, the narrower the result. An
        empty result list is a valid answer, not an error.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.
            search (str | None): Free text over number and label, case-insensitive.
            type (ProductType | None): Restricts to parts or assemblies.
            active (bool): Filters on the active status. Default: active products only.

        Returns:
            list[Product]: The matching products.
        """
        return await ProductRepository.get_products(
            session, search=search, type=type, active=active
        )

    @staticmethod
    async def create_product(product: ProductCreate, session: AsyncSession) -> Product:
        """Creates a new product in the graph.

        Args:
            product (ProductCreate): The validated data of the new product.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Product: The freshly created product.

        Raises:
            DuplicateKeyError: When the product number is already taken.
        """
        created = await ProductRepository.create_product(product, session)
        logger.bind(product_number=created.number).info("Product created.")

        await manager.send_event({
            "type": "event", "entity": "product", "trigger": "product_created",
            "reference": created.number, "ids": [created.number], "scope": "list",
        })
        return created

    @staticmethod
    async def update_product(
        number: str, update_data: ProductUpdate, session: AsyncSession
    ) -> Product:
        """Updates an existing product.

        Args:
            number (str): The unique product number.
            update_data (ProductUpdate): The data to update.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Product: The updated product.

        Raises:
            NotFoundError: When the product to update does not exist.
        """
        product = await ProductRepository.update_product(number, update_data, session)
        if product is None:
            raise NotFoundError(
                f"A product with the number '{number}' does not exist and cannot be updated."
            )

        logger.bind(product_number=number).info("Product updated.")

        # The event carries the OLD number as its reference, even when the update renamed
        # the product: that is the id every connected client currently holds, and the only
        # one they can match their local state against.
        await manager.send_event({
            "type": "event", "entity": "product", "trigger": "product_updated",
            "reference": number, "ids": [number], "scope": "list",
        })
        return product

    @staticmethod
    async def delete_product(number: str, session: AsyncSession) -> bool:
        """Deletes a product from the graph.

        Args:
            number (str): The unique product number.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            bool: True when the product was deleted.

        Raises:
            NotFoundError: When the product does not exist.
        """
        deleted = await ProductRepository.delete_product(number, session)
        if not deleted:
            raise NotFoundError(
                f"A product with the number '{number}' could not be deleted, it does not exist."
            )
        logger.bind(product_number=number).info("Product deleted.")

        await manager.send_event({
            "type": "event", "entity": "product", "trigger": "product_deleted",
            "reference": number, "ids": [number], "scope": "list",
        })
        return deleted

    @staticmethod
    async def check_product_exists(number: str, session: AsyncSession) -> bool:
        """Checks whether a product with the given number exists in the graph.

        Args:
            number (str): The unique product number.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            bool: True when it exists, otherwise False.
        """
        return await ProductRepository.check_product_exists(number, session)


class BomService:
    """Drives the business logic for assemblies and bills of materials."""

    @staticmethod
    async def get_bom(number: str, depth: int, session: AsyncSession) -> list[dict]:
        """Resolves the recursive bill of materials for an assembly.

        Args:
            number (str): Product number of the parent assembly.
            depth (int): The maximum resolution depth.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[dict]: The resolved bill of materials.

        Raises:
            NotFoundError: When the assembly does not exist.
        """
        # An empty result is ambiguous on its own: a product without a bill of materials
        # and a product that does not exist both produce no edges. The existence check
        # turns the second case into a 404 instead of an empty 200.
        exists = await ProductService.check_product_exists(number, session)
        if not exists:
            raise NotFoundError(f"A product with the number '{number}' does not exist.")

        return await BomRepository.get_bom(number, depth, session)

    @staticmethod
    async def add_component(
        number: str, line: BomLineCreate, session: AsyncSession
    ) -> dict:
        """Adds a new component to an existing assembly.

        Validates the direct self-reference up front and delegates the rest to the
        repository, which runs the parent's existence check and the edge creation
        atomically in a single transaction.

        Args:
            number (str): Product number of the parent assembly.
            line (BomLineCreate): The component data including quantity.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            dict: The data of the created bill-of-materials line.

        Raises:
            NotFoundError: When the assembly (parent) does not exist.
            BusinessLogicError: When a product would contain itself, the component does
                not exist, or a cycle was detected.
        """
        # Caught here rather than in the query, purely for the error message: the query's
        # cycle guard would reject this too, but with the generic "a cycle was detected",
        # which is a poor description of what the caller actually did.
        if number == line.componentNumber:
            raise BusinessLogicError("A product cannot contain itself as a component.")

        new_line = await BomRepository.add_component(number, line, session)
        logger.bind(product_number=number, component_number=line.componentNumber).info(
            "Component added to the bill of materials."
        )

        await manager.send_event({
            "type": "event", "entity": "product", "trigger": "bom_changed",
            "reference": number, "ids": [number, line.componentNumber], "scope": "list",
        })
        return new_line

    @staticmethod
    async def delete_component(
        number: str, component_number: str, session: AsyncSession
    ) -> BomLineDeleted:
        """Removes a component from an assembly's bill of materials.

        Only the link between the two products is deleted, not the component itself. If it
        was the last line, the repository turns the parent back into a part — visible in
        `remainingLines`.

        Args:
            number (str): Product number of the parent assembly.
            component_number (str): Product number of the component to remove.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            BomLineDeleted: The result including the number of remaining lines.

        Raises:
            NotFoundError: When there is no bill-of-materials link between the two
                products. That also covers the case where one of them does not exist at
                all — without a node there can be no edge.
        """
        deleted_line = await BomRepository.delete_component(number, component_number, session)
        if deleted_line is None:
            raise NotFoundError(
                f"Component '{component_number}' is not part of the bill of materials of "
                f"'{number}'."
            )

        logger.bind(product_number=number, component_number=component_number).info(
            "Component removed from the bill of materials."
        )

        await manager.send_event({
            "type": "event", "entity": "product", "trigger": "bom_changed",
            "reference": number, "ids": [number, component_number], "scope": "list",
        })
        return deleted_line


class CategoryService:
    """Drives the business logic of the category hierarchy."""

    @staticmethod
    async def get_hierarchy(session: AsyncSession) -> list[CategoryOption]:
        """Reads the complete category hierarchy for the selection lists.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[CategoryOption]: Every category with its product groups and
                subcategories, ordered by id.
        """
        return await CategoryRepository.get_hierarchy(session)
