"""Business logic of the assets domain, between router and repository."""

from loguru import logger
from neo4j import AsyncSession

from core.exceptions import NotFoundError
from core.websocket import manager

from .repository_assets import AssetRepository, AssetServiceRepository
from .schemas_assets import (
    Asset,
    AssetCreate,
    AssetCreated,
    AssetDraft,
    AssetDraftConfirmation,
    AssetListItem,
    AssetReleaseRequest,
    AssetReleaseResponse,
    AssetStatus,
    AssetUpdate,
    AssetUpdated,
    ComponentLineCreate,
    ComponentLineDeleted,
    InstalledComponent,
    ServiceCompletion,
    ServiceCompletionRequest,
    ServiceForecast,
    SparePartsDraft,
    SparePartsLink,
    SparePartsLinkCreated,
)


class AssetService:
    """Drives the business logic around the assets.

    The link between router and repository: the router stays free of logic, the database
    accesses stay in the repository.

    The division of labour on the error cases is not the same everywhere in this domain,
    and that has a reason:

    - **`NotFoundError` for an unknown serial number comes about here.** The repository
      delivers `None` — whether that becomes a 404 or something else is a business
      decision.
    - **Rules checking the stored state come about in the repository** — the withdrawal
      after shipping, for instance, or the wrong document type. They have to run in the
      same transaction as the write, otherwise a TOCTOU window opens. The service passes
      them through.

    Every writing operation is logged, every reading one is not. The `serialNumber` serves
    as the business key.
    """

    @staticmethod
    async def get_assets(
        session: AsyncSession,
        customerId: str | None = None,
        status: AssetStatus | None = None,
        search: str | None = None,
    ) -> list[AssetListItem]:
        """Fetches a filtered list of the assets.

        The filters are purely additive — the more are set, the narrower the result. An
        empty hit list is a valid result and no error.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.
            customerId (str | None): Narrows down to the assets of one customer.
            status (AssetStatus | None): Filters on the calculated state.
            search (str | None): Free text across serial number, internal number and
                project number.

        Returns:
            list[AssetListItem]: The matching assets in the graph.
        """
        return await AssetRepository.get_assets(
            session, customerId=customerId, status=status, search=search
        )

    @staticmethod
    async def get_asset(serial_number: str, session: AsyncSession) -> Asset:
        """Looks up an asset by its serial number.

        Args:
            serial_number (str): The business key of the asset.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Asset: The complete asset data including customer, release block and installed
                components.

        Raises:
            NotFoundError: When no asset with that serial number exists.
        """
        asset = await AssetRepository.get_asset(serial_number, session)
        if not asset:
            raise NotFoundError(f"An asset with the serial number '{serial_number}' does not exist.")
        return asset

    @staticmethod
    async def create_asset(asset: AssetCreate, session: AsyncSession) -> AssetCreated:
        """Creates a new asset on the basis of an order confirmation.

        The business checks — document type, customer on the document, uniqueness — run in
        the repository, because they have to belong to the same transaction as the
        creation. The service logs the result.

        Args:
            asset (AssetCreate): The validated input data.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            AssetCreated: The freshly created asset in state `planned`.

        Raises:
            DuplicateKeyError: When `serialNumber` or `internalNumber` is already taken.
            NotFoundError: When product or document do not exist.
            BusinessLogicError: When the document is no order confirmation or carries no
                customer.
        """
        created = await AssetRepository.create_asset(asset, session)
        logger.bind(
            serial_number=created.serialNumber,
            document_number=asset.documentNumber,
        ).info("Asset created successfully.")

        await manager.send_event({
            "type": "event", "entity": "asset", "trigger": "asset_created",
            "reference": created.serialNumber, "ids": [created.serialNumber], "scope": "list",
        })
        return created

    @staticmethod
    async def set_release(
        serial_number: str,
        release: AssetReleaseRequest,
        employee_id: str,
        session: AsyncSession,
    ) -> AssetReleaseResponse:
        """Grants the bill-of-materials release or withdraws it.

        Logs both directions distinguishably: the release is a formally relevant act, and
        who released — or took it back — when has to stay traceable.

        Args:
            serial_number (str): The business key of the asset.
            release (AssetReleaseRequest): Direction and note.
            employee_id (str): Id of the releasing employee, from the auth token.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            AssetReleaseResponse: The new state including the release block.

        Raises:
            NotFoundError: When the asset or the employee does not exist.
            BusinessLogicError: On a withdrawal after shipping, or when the stock does not
                suffice.
        """
        response = await AssetRepository.set_release(
            serial_number, release, employee_id, session
        )
        if not response:
            raise NotFoundError(f"An asset with the serial number '{serial_number}' does not exist.")

        # Record both directions distinguishably: the release is a formally relevant act,
        # and who released — or took it back — when has to stay traceable.
        logger.bind(
            serial_number=serial_number,
            employee_id=employee_id,
        ).info(
            "Bill-of-materials release granted."
            if release.released
            else "Bill-of-materials release withdrawn."
        )

        await manager.send_event({
            "type": "event", "entity": "asset",
            "trigger": "release_granted" if release.released else "release_withdrawn",
            "reference": serial_number, "ids": [serial_number], "scope": "list",
        })
        return response

    @staticmethod
    async def update_asset(
        serial_number: str,
        update_data: AssetUpdate,
        session: AsyncSession,
    ) -> AssetUpdated:
        """Maintains the shipping and installation date of an asset.

        With the shipping date the service interval begins. The answer reports the number
        of components created, and the log entry should carry it too — otherwise it cannot
        be established afterwards when a twin came about.

        Args:
            serial_number (str): The business key of the asset.
            update_data (AssetUpdate): The fields to change.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            AssetUpdated: The complete asset including the number of newly created
                component instances.

        Raises:
            NotFoundError: When no asset with that serial number exists.
            BusinessLogicError: On an empty request or the attempt to take back a shipping
                date already set.
        """
        updated = await AssetRepository.update_asset(serial_number, update_data, session)
        if not updated:
            raise NotFoundError(f"An asset with the serial number '{serial_number}' does not exist.")

        # The number of components created belongs in the log: otherwise it cannot be
        # established afterwards when the digital twin of an asset came about.
        logger.bind(
            serial_number=serial_number,
            created_components=updated.createdComponents,
        ).info("Asset updated.")

        await manager.send_event({
            "type": "event", "entity": "asset", "trigger": "asset_changed",
            "reference": serial_number, "ids": [serial_number], "scope": "list",
        })
        return updated

    @staticmethod
    async def get_bom(serial_number: str, session: AsyncSession) -> list[InstalledComponent]:
        """Delivers the bill of materials of a single asset.

        Args:
            serial_number (str): The business key of the asset.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[InstalledComponent]: The components, empty when there are none.

        Raises:
            NotFoundError: When no asset with that serial number exists.
        """
        components = await AssetRepository.get_bom(serial_number, session)
        if components is None:
            raise NotFoundError(f"An asset with the serial number '{serial_number}' does not exist.")
        return components

    @staticmethod
    async def set_component(
        serial_number: str, line: ComponentLineCreate, session: AsyncSession
    ) -> dict:
        """Adds a component or changes its quantity.

        Args:
            serial_number (str): The business key of the asset.
            line (ComponentLineCreate): Product number and quantity.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            dict: The fields of `InstalledComponent` plus `was_updated`.

        Raises:
            NotFoundError: When the asset or the product does not exist.
            BusinessLogicError: When the bill of materials is already released.
        """
        result = await AssetRepository.set_component(serial_number, line, session)
        if result is None:
            raise NotFoundError(f"An asset with the serial number '{serial_number}' does not exist.")

        logger.bind(serial_number=serial_number, product_number=line.productNumber).info(
            "Bill-of-materials line of the asset changed."
        )
        await manager.send_event({
            "type": "event", "entity": "asset", "trigger": "bom_changed",
            "reference": serial_number, "ids": [serial_number], "scope": "list",
        })
        return result

    @staticmethod
    async def delete_component(
        serial_number: str, product_number: str, session: AsyncSession
    ) -> ComponentLineDeleted:
        """Strikes a component from the bill of materials of an asset.

        Args:
            serial_number (str): The business key of the asset.
            product_number (str): Product number of the component to strike.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            ComponentLineDeleted: The result including the remaining lines.

        Raises:
            NotFoundError: When the asset or this component does not exist.
            BusinessLogicError: When the bill of materials is already released.
        """
        result = await AssetRepository.delete_component(serial_number, product_number, session)
        if result is None:
            raise NotFoundError(f"An asset with the serial number '{serial_number}' does not exist.")

        logger.bind(serial_number=serial_number, product_number=product_number).info(
            "Bill-of-materials line of the asset struck."
        )
        await manager.send_event({
            "type": "event", "entity": "asset", "trigger": "bom_changed",
            "reference": serial_number, "ids": [serial_number], "scope": "list",
        })
        return result

    @staticmethod
    async def get_asset_drafts(session: AsyncSession) -> list[AssetDraft]:
        """Fetches the open asset drafts (reading, no log entry).

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[AssetDraft]: One row per open document.
        """
        return await AssetRepository.get_asset_drafts(session)

    @staticmethod
    async def confirm_draft(
        confirmation: AssetDraftConfirmation,
        employee_id: str,
        session: AsyncSession,
    ) -> list[AssetCreated]:
        """Confirms an asset draft: creates the asset and marks it as released, in one
        step.

        Args:
            confirmation (AssetDraftConfirmation): Document, bill of materials and note.
            employee_id (str): Id of the confirming employee, from the auth token.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[AssetCreated]: The newly created, already released asset.

        Raises:
            NotFoundError: When document or employee do not exist.
            BusinessLogicError: When the draft cannot be confirmed (see
                `AssetRepository.confirm_draft`).
        """
        created = await AssetRepository.confirm_draft(confirmation, employee_id, session)

        logger.bind(
            document_number=confirmation.documentNumber,
            serial_numbers=[c.serialNumber for c in created],
            employee_id=employee_id,
        ).info("Asset draft confirmed.")

        await manager.send_event({
            "type": "event", "entity": "asset", "trigger": "draft_confirmed",
            "reference": confirmation.documentNumber,
            "ids": [c.serialNumber for c in created], "scope": "list",
        })
        return created

    @staticmethod
    async def get_spare_parts_drafts(session: AsyncSession) -> list[SparePartsDraft]:
        """Fetches the open spare-parts drafts (reading, no log entry).

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[SparePartsDraft]: One row per open document.
        """
        return await AssetRepository.get_spare_parts_drafts(session)

    @staticmethod
    async def link_spare_parts(
        document_number: str,
        link: SparePartsLink,
        session: AsyncSession,
    ) -> SparePartsLinkCreated:
        """Links a spare-parts document with existing assets.

        Args:
            document_number (str): Number of the spare-parts document.
            link (SparePartsLink): Serial numbers of the target assets.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            SparePartsLinkCreated: The linked serial numbers.

        Raises:
            NotFoundError: When the document or one of the assets named does not exist.
        """
        result = await AssetRepository.link_spare_parts(document_number, link, session)
        if result is None:
            raise NotFoundError(f"Document '{document_number}' does not exist.")

        logger.bind(
            document_number=document_number, serial_numbers=link.serialNumbers,
        ).info("Spare-parts document linked with asset(s).")

        await manager.send_event({
            "type": "event", "entity": "asset", "trigger": "spare_parts_linked",
            "reference": document_number, "ids": link.serialNumbers, "scope": "list",
        })
        return result


class AssetServiceForecastService:
    """Service layer for the service forecast of the installed wear parts.

    Acts as the mediator between the API router and the database repository. Encapsulates
    the business logic for determining the services falling due and can later be extended
    with validations or permission checks.
    """

    @staticmethod
    async def get_due_services(session: AsyncSession) -> list[ServiceForecast]:
        """Fetches the list of due services from the repository.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[ServiceForecast]: A list of all wear parts that have to be replaced,
                including their context.
        """
        return await AssetServiceRepository.get_due_services(session)

    @staticmethod
    async def set_completion(
        component_instance_id: str,
        completion: ServiceCompletionRequest,
        session: AsyncSession,
    ) -> ServiceCompletion:
        """Reports a service as done.

        Args:
            component_instance_id (str): Id of the `ComponentInstance`
                ('serialNumber_productNumber'), from `ServiceForecast.componentInstanceId`.
            completion (ServiceCompletionRequest): Technician initials and note.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            ServiceCompletion: The stored state of the completion report.

        Raises:
            NotFoundError: When no component instance with that id exists.
        """
        result = await AssetServiceRepository.set_completion(
            component_instance_id, completion, session
        )
        if result is None:
            raise NotFoundError(
                f"A component instance with the id '{component_instance_id}' does not exist."
            )

        logger.bind(component_instance_id=component_instance_id).info(
            "Service reported as done."
        )

        await manager.send_event({
            "type": "event", "entity": "service", "trigger": "service_completed",
            "reference": component_instance_id, "ids": [component_instance_id],
            "scope": "list",
        })
        return result
