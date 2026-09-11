"""Business logic of the notifications domain — service, shortage, unavailable quantity."""

from datetime import UTC, datetime

from loguru import logger
from neo4j import AsyncSession

from core.exceptions import NotFoundError
from core.websocket import manager

from .repository_notifications import NotificationRepository
from .schemas_notifications import Notification, UnavailableReport


class NotificationService:
    """The link between router and repository, plus the control of the annual run."""

    @staticmethod
    async def get_notifications(
        roles: list[str] | None, done: bool | None, session: AsyncSession
    ) -> list[Notification]:
        """Delivers the notification list once the annual run is assured.

        **No scheduler in the backend:** instead of a cron job, this reading call itself
        creates the service list of the current calendar year idempotently before it reads
        — on the first request in the new year the rows come about by themselves, on every
        further call it is a no-op thanks to `MERGE`. The price: `GET /api/notifications`
        occasionally writes instead of being purely reading. That is a deliberate trade
        against a new piece of infrastructure, not a side-effect accident.

        Args:
            roles (list[str] | None): Roles from the caller's token, or None for an
                unfiltered list (Admin).
            done (bool | None): Only ticked-off/open notifications, or None for both.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[Notification]: The notifications found, newest first.
        """
        await NotificationRepository.create_annual_service_list(
            datetime.now(UTC).year, session
        )
        return await NotificationRepository.get_notifications(roles, done, session)

    @staticmethod
    async def get_target_role(id: str, session: AsyncSession) -> str:
        """Reads the target role of a notification for the role check in the router.

        Args:
            id (str): Business key of the notification.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            str: The name of the target role.

        Raises:
            NotFoundError: When no notification with that id exists.
        """
        target_role = await NotificationRepository.get_target_role(id, session)
        if target_role is None:
            raise NotFoundError(f"A notification with the id '{id}' does not exist.")
        return target_role

    @staticmethod
    async def set_done(id: str, session: AsyncSession) -> Notification:
        """Ticks a notification off.

        Args:
            id (str): Business key of the notification.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Notification: The updated notification.

        Raises:
            NotFoundError: When no notification with that id exists.
        """
        result = await NotificationRepository.set_done(id, session)
        if result is None:
            raise NotFoundError(f"A notification with the id '{id}' does not exist.")
        logger.bind(notification_id=id).info("Notification ticked off.")
        await manager.send_event({
            "type": "event", "entity": "notification", "trigger": "ticked_off",
            "reference": id, "ids": [id], "scope": "list",
        })
        return result

    @staticmethod
    async def report_unavailable(
        report: UnavailableReport, session: AsyncSession
    ) -> list[Notification]:
        """Reports quantities of a delivery note missing at the location to purchasing.

        Args:
            report (UnavailableReport): Document number and the lines concerned.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[Notification]: The reports created, respectively carried forward.

        Raises:
            NotFoundError: When the document does not exist or a line number does not
                belong to it.
        """
        result = await NotificationRepository.report_unavailable(
            report.documentNumber, report.lines, session
        )
        logger.bind(document_number=report.documentNumber, count=len(result)).info(
            "Unavailable quantities reported to purchasing."
        )
        # `_created` is not merely cosmetic here: on a reported key it does not know yet,
        # the event receiver in a client would otherwise not reload at all. For purchasing
        # a new report is unknown by definition.
        await manager.send_event({
            "type": "event", "entity": "notification", "trigger": "unavailable_created",
            "reference": report.documentNumber,
            "ids": [n.id for n in result], "scope": "list",
        })
        return result

    @staticmethod
    async def create_annual_service_list(year: int, session: AsyncSession) -> int:
        """Creates the service list of a year manually (`POST .../annual-run`, `Admin`).

        The same function `get_notifications` already calls automatically — offered
        explicitly here for an external cron or a manual catch-up, for a past year for
        instance.

        Args:
            year (int): The calendar year the list is created for.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            int: Number of notifications belonging to that year (new and already present
                alike).
        """
        count = await NotificationRepository.create_annual_service_list(year, session)
        logger.bind(year=year, count=count).info("Annual run of the service list carried out.")
        if count:
            await manager.send_event({
                "type": "event", "entity": "notification", "trigger": "annual_run",
                "reference": str(year), "ids": [], "scope": "many",
            })
        return count
