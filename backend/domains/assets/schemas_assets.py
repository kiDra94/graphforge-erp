"""Pydantic schemas of the assets domain: read, write and response models."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.neo4j_types import Neo4jDate, Neo4jDatetime
from core.schemas import InputModel

# The three states of an asset along its course: created -> engineering releases the bill
# of materials -> the asset ships.
#
# The value is NOT a property on the node, it is derived in Cypher from `shippedOn` and
# `bomReleased`. Storing it would mean keeping two sources for the same fact in sync, and
# the two properties that decide it have to be maintained anyway.
#
# Typed as a `Literal`, so FastAPI rejects an unknown value in the query parameter with a
# 422 instead of silently answering it with an empty result list.
AssetStatus = Literal["planned", "released", "shipped"]


class ServiceCompletion(BaseModel):
    """The stored state of a completed service — one `ServiceEvent` node.

    The forecast itself (`ServiceForecast`) stays a calculation, but it needs an anchor it
    remembers: without one the next replacement date would stand at the original shipping
    date for ever, and a component falling due again after the swap would never be
    reported. `completedAt` takes that role from the first report onwards (see
    `AssetServiceRepository.get_due_services`). A slim node per component, hung off the
    `ComponentInstance`, is enough for that — no planning, no life cycle of its own, no
    history of several reports.
    """
    completedAt: Neo4jDate = Field(
        description="Date the service was reported as done. Set server-side on reporting."
    )
    technicianInitials: str | None = Field(
        default=None,
        description="Initials of the employee who carried out the service. 'null' when none were given."
    )
    note: str | None = Field(
        default=None,
        description="Free text on the completion, for instance anything noticed during the swap. 'null' means 'nothing recorded'."
    )


class ServiceForecast(BaseModel):
    """Output schema of one due service.

    Represents a service case for a wear part inside an asset, including the link to the
    customer and the responsible account manager.

    The due date is calculated from the asset's shipping date, not from the component's
    installation date: the service interval runs from the point of sale. As soon as a
    service has been reported (`completion` is set), the next cycle counts from its
    `completedAt` instead — otherwise the date would stay the same for ever. Both dates
    (`installedOn`, `replacementDueOn`) are part of the answer because they answer
    different questions: `installedOn` says what sits in the machine, `replacementDueOn`
    says when it has to be swapped.
    """
    componentInstanceId: str = Field(
        description="Id of the underlying ComponentInstance ('serialNumber_productNumber'). Addresses this service case uniquely for PUT /api/assets/service/{componentInstanceId}/completion."
    )
    accountManager: str = Field(
        description="Name of the responsible account manager. Reads 'Unassigned' when no employee is assigned."
    )
    customer: str = Field(
        description="Name of the customer the asset belongs to."
    )
    serialNumber: str = Field(
        description="Serial number of the affected asset."
    )
    wearPart: str = Field(
        description="Label of the affected wear part, that is of the product behind the installed component."
    )
    installedOn: Neo4jDate | None = Field(
        default=None,
        description="Installation date of this particular component in this asset. May be missing, because it is usually unknown at shipping time and only follows once the asset gets its own installation date."
    )
    replacementDueOn: Neo4jDate = Field(
        description="The calculated due date for the replacement: shipping date of the asset plus the service interval of the wear part, or — once reported — completion date of the last report plus the interval."
    )
    completion: ServiceCompletion | None = Field(
        default=None,
        description="The last stored completion report, if there is one already. Because it doubles as the anchor of the currently shown due date, a record with a set `completion` is no contradiction: it reports the next cycle, not the same one again."
    )


class ServiceCompletionRequest(InputModel):
    """Schema for reporting a service as done (PUT .../completion).

    `completedAt` is deliberately absent: the server sets the date itself (`date()`) on
    reporting, the same way it does with `releasedOn` on a bill-of-materials release — a
    client must not be able to claim a completion date retroactively.

    Reporting the same component again overwrites the previous report (MERGE onto the one
    node per component) and thereby moves the next replacement date along:
    `get_due_services` then counts from this `completedAt` and no longer from the asset's
    shipping date. There is deliberately no history of several completions — only the
    current anchor point.
    """
    technicianInitials: str | None = Field(
        default=None,
        description="Initials of the employee who carried out the service, for instance 'MF'. Not checked against the employee list — a display field, like in the dialog it comes from."
    )
    note: str | None = Field(
        default=None,
        description="Free text on the completion, for instance 'battery visibly swollen when swapped'."
    )


class AssetBase(BaseModel):
    """The business field set of the read model — deliberately kept tolerant.

    Its only heir is `Asset`, which adds the server-maintained timestamps, the calculated
    `status` and the fields resolved through edges, and changes not a single type. The
    write models `AssetCreate` and `AssetUpdate` deliberately do NOT inherit from here:
    they tighten types (`str | None` -> `str`) and carry fields that are no properties at
    all (`documentNumber`, `productNumber`). A derivation that changes inherited types is
    no longer a subtype. Pydantic carries it at runtime, type checkers reject it rightly.

    Not here are the three release properties `bomReleased`, `releasedOn` and
    `releaseNote`, although they sit on the node. They are grouped in `AssetRelease` — the
    reasoning is over there.

    The only mandatory field is `serialNumber`. It is the business key, carries the
    constraint `asset_serialnumber` and doubles as the path parameter; without it the
    answer would not be addressable.

    Every other field is optional and carries NO value constraints.

    That is intentional: as the base of the read model (`Asset`), a constraint here would
    protect nothing, it would merely turn an already stored database state into an HTTP
    500. The graph does hold exactly such nodes — not every asset carries a `shippedOn` or
    an `installedOn`.

    Value validation belongs at the system boundary and therefore happens exclusively in
    the write models `AssetCreate` and `AssetUpdate`.
    """
    serialNumber: str = Field(
        description="Business key of the asset, for instance 'SN-2026-0001'. Formed server-side and only checked for uniqueness, not for format."
    )
    internalNumber: str | None = Field(
        default=None,
        description="Internal asset number, for instance 'ACME-SN-0001'. A second number assigned independently of the serial number."
    )
    projectNumber: str | None = Field(
        default=None,
        description="Project number of the order the asset belongs to, for instance '2026-0001'."
    )
    orderedOn: Neo4jDate | None = Field(
        default=None,
        description="Date the customer ordered the asset."
    )
    shippedOn: Neo4jDate | None = Field(
        default=None,
        description="Date of shipping to the customer. Start of the service interval."
    )
    installedOn: Neo4jDate | None = Field(
        default=None,
        description="Date the asset was installed at the customer's site. Not to be confused with the installation date of a single component; the service interval runs from the shipping date."
    )


class AssetRelease(BaseModel):
    """The release block of the answer — grouping everything that belongs to the release
    of the bill of materials.

    The three properties `bomReleased`, `releasedOn` and `releaseNote` deliberately do NOT
    sit flat in `AssetBase`, although they are on the node. They would otherwise appear
    twice in the detail answer — once flat and once here —, and a client would have two
    sources for the same state.

    `employee` is no property, it is the name behind the edge `RELEASED_BY`. It belongs to
    the release and nowhere else: the release is a formally relevant act, and who released
    has to stay traceable.

    Every field except `released` is optional. After a withdrawal they are empty, and the
    graph holds assets without the `RELEASED_BY` edge.
    """
    released: bool = Field(
        default=False,
        description="Engineering has released the bill of materials, the asset may be built. A planning release — the machine does not exist yet at that point. Corresponds to the property 'bomReleased' on the node."
    )
    releasedOn: Neo4jDate | None = Field(
        default=None,
        description="Date of the bill-of-materials release. 'null' after a withdrawal."
    )
    employee: str | None = Field(
        default=None,
        description="Name of the employee who released, resolved through the edge 'RELEASED_BY'. 'null' when the edge is missing or the release was withdrawn."
    )
    note: str | None = Field(
        default=None,
        description="Free text of the employee on the release. 'null' means 'nothing recorded', an empty string 'deliberately left blank'."
    )


class AssetCustomer(BaseModel):
    """The customer as it appears in an asset answer: id and name only.

    The `Customer` node carries exactly these two properties; a short name does not exist
    in the graph. Deliberately a schema of its own rather than the richer `Customer` of
    the sales domain — an asset answer would otherwise drag along addresses and payment
    terms nobody reads there.
    """
    id: str = Field(description="Customer number, for instance 'C-1001'.")
    name: str = Field(description="Name of the customer.")


class InstalledComponent(BaseModel):
    """One component of the digital twin — a `ComponentInstance` with its product.

    Answers what is actually built into the machine at the customer's site. The path in
    the graph is `AssetInstance -[:HAS_COMPONENT]-> ComponentInstance -[:IS_TYPE]->
    Product`.

    `productNumber` is there because `label` is display text and no key. It addresses the
    component uniquely: `ComponentInstance.id` is composed as `serialNumber + '_' +
    productNumber`, so a future component swap needs only these two business values and
    never the technical id.

    `status` is not included. The query already filters on `'active'` — a field carrying
    the same value in every row says nothing. Once component swaps exist and replaced
    instances are meant to ship along, it belongs here.
    """
    productNumber: str = Field(
        description="Product number of the installed component, resolved through the edge 'IS_TYPE'."
    )
    label: str | None = Field(
        default=None,
        description="Label of the product. Can be missing on thinly maintained products."
    )
    quantity: Decimal = Field(
        description="Required quantity of this component in this asset, held on the edge 'HAS_COMPONENT'. Its own quantity, copied from the standard bill of materials and independently changeable afterwards."
    )
    installedOn: Neo4jDate | None = Field(
        default=None,
        description="Installation date of this component. Usually 'null' at shipping time and only filled once the asset gets its installation date."
    )


class AssetSystemFields(BaseModel):
    """Mix-in schema for the system-managed timestamps.

    Holds the administrative metadata (creation and update time) which is generated and
    maintained exclusively server-side in UTC. They are not meant as input fields for the
    client.
    """
    createdAt: Neo4jDatetime | None = Field(
        default=None,
        description="The creation timestamp in UTC. Set server-side in the repository on creation; it can be missing on nodes from legacy stock or imports."
    )
    updatedAt: Neo4jDatetime | None = Field(
        default=None, description="The timestamp of the last update."
    )


class AssetCreate(InputModel):
    """Schema for creating (POST) a new asset.

    Tightens the field set at the system boundary: what a client creates has to be
    complete and plausible — even though historically incomplete nodes exist in the graph
    which the read model has to tolerate.

    Declares its fields itself instead of inheriting from `AssetBase`. The tightening
    `str | None` -> `str` would, as inheritance, break the substitution principle; the
    repeated lines are the price for read and write contract staying independently
    changeable.

    The field set is deliberately smaller than `AssetBase`:

    - `documentNumber` and `productNumber` are no properties, they are the targets of the
      edges `BASED_ON_DOCUMENT` and `BASED_ON`. They stand here because creation needs
      them; in the read model they appear as derived fields.
    - **`serialNumber` is missing.** The server forms it itself — see below. The client
      learns it from the answer (`AssetCreated`).
    - `customerId` is deliberately missing. The customer is resolved through
      `Document -[:BELONGS_TO_CUSTOMER]-> Customer`. Were it sent separately, asset and
      document could point at different customers without anyone noticing.
    - `projectNumber` and `orderedOn` are missing for the same reason: both sit on the
      document, respectively on the order behind it, and are taken over server-side.
    - The release fields belong to `PATCH /api/assets/{serialNumber}/release`, `shippedOn`
      and `installedOn` to `PATCH /api/assets/{serialNumber}`. A freshly created asset is
      therefore always in status `planned`.

    **How the serial number comes about:** `SN-{projectNumber}[-n]`, formed from the
    project number of the order behind the order confirmation. Does the same order already
    carry assets, the server appends a running number (`-2`, `-3`, …). Once assigned, the
    number never changes again. The full derivation sits at `AssetRepository.create_asset`.

    Careful when extending: a new field in `AssetBase` does NOT land here automatically.
    Whether it is needed on creation is a separate decision every time — the same holds
    for `AssetUpdate`.
    """
    documentNumber: str = Field(
        description="Number of the order confirmation the asset comes out of, for instance 'OC-2026-0001'. Every other document type is rejected; from the document the server derives customer and order."
    )
    productNumber: str | None = Field(
        default=None,
        description="Product number of a matching catalogue product, if there is one, for instance 'ACME-1000'. Optional — most assets come about without one, their bill of materials then comes exclusively from 'components'/the draft flow. Only when set does 'BASED_ON' come about, and only then is the standard bill of materials of that product copied in the absence of own 'components'."
    )
    internalNumber: str | None = Field(
        default=None,
        description="Internal asset number, for instance 'ACME-SN-0005'. Optional — but if given it has to be unique as well."
    )


class AssetUpdate(InputModel):
    """Schema for updating (PATCH) an asset.

    Every field is optional, because a PATCH request only has to carry the fields that
    actually change (partial update).

    Deliberately carries only the two date fields:

    - `serialNumber` is not here. It is the key and at the same time the path parameter;
      renaming it through the very call that addresses it would be ambiguous.
    - The release fields belong to the release endpoint. That one additionally maintains
      the edge `RELEASED_BY` and knows a withdrawal — more than a field update can do.
    - `documentNumber` and `productNumber` are missing, because changing the document
      would move customer and order along with it, and changing the product would move
      the bill of materials an already created digital twin came out of.

    `shippedOn` is no ordinary field: set for the first time, it starts the service
    interval and the status switches to `shipped`.
    """
    shippedOn: Neo4jDate | None = Field(
        default=None,
        description="Date of shipping to the customer. Starts the service interval when set for the first time."
    )
    installedOn: Neo4jDate | None = Field(
        default=None,
        description="Date the asset was installed at the customer's site. Carried over to the component instances that already exist."
    )


class AssetReleaseRequest(InputModel):
    """Schema for the bill-of-materials release and its withdrawal (PATCH .../release).

    One schema for both directions, because `released` decides the direction:

    - `true` sets `bomReleased`, `releasedOn` and `releaseNote` and draws the edge
      `RELEASED_BY`.
    - `false` is the withdrawal. It resets the same fields and removes the edge. Later
      changes to the bill of materials have to stay possible despite a release.

    The status change happens without logic of its own — it follows from the derivation
    rule as soon as `bomReleased` changes. That is exactly what `status` is calculated and
    not stored for.

    A withdrawal after shipping is rejected with 400. That is checked by the repository
    against the stored state, not by this schema: the request alone does not carry the
    information whether the asset has already shipped.
    """
    released: bool = Field(
        description="'true' releases the bill of materials, 'false' withdraws an existing release."
    )
    note: str | None = Field(
        default=None,
        description="Free text on the release, for instance 'checked, all dimensions verified'. Only allowed on a release."
    )

    @model_validator(mode="after")
    def note_only_on_release(self) -> AssetReleaseRequest:
        """Rejects a note sent together with a withdrawal.

        The withdrawal deletes `releaseNote` — a remark on a retracted check has no
        reference any more. Without this check the text sent along would vanish silently,
        and the client would believe it stored.
        """
        if not self.released and self.note is not None:
            raise ValueError(
                "note is not allowed on a withdrawal: the release note is deleted by it."
            )
        return self


class ComponentLineCreate(InputModel):
    """Schema for adding or changing one line of the bill of materials (POST
    `/api/assets/{serialNumber}/bom`).

    Mirrored on `BomLineCreate` (`domains.catalog.schemas_catalog`), so that no second
    idiom comes about for the asset's bill of materials beside the BOM pattern that
    already exists in `catalog`. Unlike there, `productNumber` addresses the component
    directly here — there is no intermediate step over a third number.

    Does the component already exist on this asset, the call only overwrites `quantity`
    (`MERGE`, 200 instead of 201). After the release the call is locked (400) — otherwise
    the bill of materials could be changed after stock had already been reserved for it.
    """
    productNumber: str = Field(
        min_length=1,
        description="Product number of the component that is added, or whose quantity is changed."
    )
    quantity: Decimal = Field(
        gt=0, description="Required quantity of the component in this asset."
    )


class ComponentLineDeleted(BaseModel):
    """Schema for the successful deletion of a bill-of-materials line (DELETE
    `/api/assets/{serialNumber}/bom/{productNumber}`).

    Mirrored on `BomLineDeleted`. `remainingLines` says whether the asset still carries a
    bill of materials afterwards — `0` means "with no components at all", which is
    admissible in business terms (engineering has struck everything) but rarely the normal
    case.
    """
    serialNumber: str = Field(description="The business key of the asset.")
    productNumber: str = Field(description="Product number of the component struck.")
    remainingLines: int = Field(
        ge=0, description="Number of components remaining after the deletion."
    )


class AssetListItem(BaseModel):
    """One entry of the asset list (GET /api/assets).

    Deliberately narrower than `Asset` and without inheriting from `AssetBase`: the list
    carries only what an overview needs. Whoever wants more fetches the asset singly.
    Inheriting from `AssetBase` and removing fields again is not possible in Pydantic
    anyway.

    `productNumber`, `customerId` and `documentNumber` are no properties, they are the
    targets of the edges `BASED_ON`, `SOLD_TO` and `BASED_ON_DOCUMENT`.
    """
    serialNumber: str = Field(description="Business key of the asset, for instance 'SN-2026-0001'.")
    internalNumber: str | None = Field(
        default=None, description="Internal asset number, for instance 'ACME-SN-0001'."
    )
    productNumber: str | None = Field(
        default=None,
        description="Product number of the sold asset through the edge 'BASED_ON', for instance 'ACME-1000'."
    )
    status: AssetStatus = Field(description="Calculated state of the asset.")
    customerId: str | None = Field(
        default=None,
        description="Customer number through the edge 'SOLD_TO'. The id is enough in the list; the name comes with the detail answer."
    )
    documentNumber: str | None = Field(
        default=None,
        description="Number of the order confirmation through the edge 'BASED_ON_DOCUMENT'. 'null' on assets from legacy stock that do not carry this edge."
    )
    installedOn: Neo4jDate | None = Field(
        default=None,
        description="Date the asset was installed at the customer's site."
    )


class Asset(AssetBase, AssetSystemFields):
    """Schema for the output (GET) of a single asset.

    Inherits from `AssetBase` and `AssetSystemFields` and adds everything that is no
    property on the node:

    - `status` is calculated from `shippedOn` and `bomReleased`.
    - `productNumber`, `customer` and `documentNumber` follow the edges `BASED_ON`,
      `SOLD_TO` and `BASED_ON_DOCUMENT`.
    - `release` bundles the three release properties with the name behind `RELEASED_BY`.
    - `components` is the as-built state: the digital twin over `HAS_COMPONENT`.

    All five are read-only. They stand neither in `AssetCreate` nor in `AssetUpdate`,
    because they have no state of their own a client could set here — `status` follows
    from two properties, the edge fields from the graph, the release has its own endpoint,
    and the twin comes about with the bill of materials.

    `status` deliberately carries NO default. The map projection in the repository
    (`asset_projection()`) delivers it on every query; a default would not let a caller
    who forgets the projection fail, it would silently report an already shipped asset as
    `planned`.

    `release` and `components` do carry defaults, because both empty states are the normal
    case: an asset before the release has no releaser, and an asset without a bill of
    materials has no twin. The empty block, respectively the empty list, is then the right
    answer and no error.

    `components` stands only here and not in `AssetListItem`: for an overview list the
    query would have to descend into the twin per asset, and none of it would be shown.
    """
    status: AssetStatus = Field(
        description="State of the asset derived from `shippedOn` and `bomReleased`. Not on the node, calculated in the query."
    )
    productNumber: str | None = Field(
        default=None,
        description="Product number of the sold asset through the edge 'BASED_ON'. Determines the bill of materials the digital twin comes out of."
    )
    customer: AssetCustomer | None = Field(
        default=None,
        description="The customer through the edge 'SOLD_TO'."
    )
    documentNumber: str | None = Field(
        default=None,
        description="Number of the order confirmation through the edge 'BASED_ON_DOCUMENT'. 'null' on assets from legacy stock."
    )
    release: AssetRelease = Field(
        default_factory=AssetRelease,
        description="State of the bill-of-materials release including the releasing employee."
    )
    components: list[InstalledComponent] = Field(
        default_factory=list,
        description="As-built state: the components currently installed, over 'HAS_COMPONENT'. Empty as long as the asset carries no bill of materials."
    )

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "serialNumber": "SN-2026-0001",
                "internalNumber": "ACME-SN-0001",
                "productNumber": "ACME-1000",
                "projectNumber": "2026-0001",
                "status": "shipped",
                "customer": {"id": "C-1001", "name": "Northwind Systems GmbH"},
                "documentNumber": "OC-2026-0001",
                "release": {
                    "released": True,
                    "releasedOn": "2026-03-10",
                    "employee": "Dana Weber",
                    "note": None
                },
                "orderedOn": "2026-01-22",
                "shippedOn": "2026-03-18",
                "installedOn": "2026-03-20",
                "components": [
                    {
                        "productNumber": "ACME-2001",
                        "label": "Sealing Ring 40mm",
                        "quantity": 2,
                        "installedOn": "2026-03-20"
                    },
                    {
                        "productNumber": "ACME-2003",
                        "label": "Control Board R2",
                        "quantity": 1,
                        "installedOn": "2026-03-20"
                    }
                ],
                "createdAt": None,
                "updatedAt": None
            }
        }
    )


class AssetCreated(BaseModel):
    """Answer to `POST /api/assets` (201).

    Deliberately three fields instead of the full `Asset`: the client knows the rest of
    the body, it has just sent it. What is interesting is what the server contributed.

    **`serialNumber` is the actual purpose of this answer.** It is not in the request —
    the server forms it from the project number of the order behind the order
    confirmation. Without it the client would not know under which key to find the new
    asset again.

    `status` is always `planned` here; the field is included anyway, so the frontend does
    not have to guess and the answer has the same shape as everywhere else.
    """
    serialNumber: str = Field(
        description="The serial number assigned by the server, for instance 'SN-2026-0001'."
    )
    internalNumber: str | None = Field(
        default=None, description="Internal asset number, if given on creation."
    )
    status: AssetStatus = Field(
        description="State right after creation — 'planned', or 'released' when it comes out of a confirmed draft."
    )


class AssetReleaseResponse(BaseModel):
    """Answer to `PATCH /api/assets/{serialNumber}/release`.

    Kept narrow: the caller changed exactly one aspect of the asset and needs back what
    came out of it — the new status and the release block.

    After a withdrawal `status` stands at `planned` and `release` carries nothing but
    `released: false` and `null` values.
    """
    serialNumber: str = Field(description="Business key of the asset.")
    status: AssetStatus = Field(description="State after the release, respectively the withdrawal.")
    release: AssetRelease = Field(description="The release block after the change.")


class AssetUpdated(Asset):
    """Answer to `PATCH /api/assets/{serialNumber}`.

    The complete asset plus the number of component instances created in the process.

    The number is in the answer because two cases would otherwise be indistinguishable:
    does the product carry no components, no twin comes about — that is the normal case
    for simple assets and no error. Without the figure the frontend could not tell
    "nothing to do" from "something went wrong".

    `0` also stands there when the call set no shipping date at all or the twin already
    existed: the `MERGE` creates nothing twice.
    """
    createdComponents: int = Field(
        default=0,
        ge=0,
        description="Number of component instances of the digital twin newly created by this call."
    )


# --- Asset drafts --------------------------------------------------------------------
# Merges "create asset" and "release bill of materials" into a single step: an
# AssetInstance used to come about automatically and immediately with the order
# confirmation, carrying a still unconfirmed copy of the standard bill of materials. Now
# sales additionally decides through Document.assetPurpose
# (domains.sales.schemas_sales) whether an asset is meant at all, and the instance only
# comes about once engineering confirms the (possibly adjusted) bill of materials — see
# AssetRepository.get_asset_drafts / confirm_draft.
#
# There is no catalogue product representing a whole asset — the bill of materials of a
# new draft therefore comes exclusively from the document's own lines (editable by
# engineering), never from a product BOM.


class DraftLine(BaseModel):
    """One document line of an asset draft (see `AssetDraft.lines`) — the starting point
    for the editable bill of materials engineering sends along on confirmation."""
    productNumber: str = Field(description="Product number of the line.")
    label: str | None = Field(default=None, description="Label of the product, for display.")
    quantity: Decimal = Field(description="Ordered quantity of this line.")


class AssetDraft(BaseModel):
    """One entry of the open asset drafts (GET /api/assets/drafts/assets).

    One row per order confirmation with `assetPurpose == 'newAsset'` that does not carry
    an `AssetInstance` yet (`BASED_ON_DOCUMENT`). `lines` lists all lines of the document
    — engineering picks on confirmation which of them become (possibly adjusted) the bill
    of materials of the new asset.
    """
    documentNumber: str = Field(
        description="Number of the order confirmation, for instance 'OC-2026-0001'."
    )
    customer: AssetCustomer | None = Field(
        default=None, description="Customer of the order confirmation."
    )
    lines: list[DraftLine] = Field(
        description="All non-cancelled lines of the document, as a proposal for the bill of materials."
    )


class AssetDraftConfirmation(InputModel):
    """Schema for confirming an asset draft (POST `/api/assets/drafts/assets/confirm`).

    Merges `POST /api/assets` and `PATCH .../release` into one atomic step: creates an
    `AssetInstance`, writes `components` as its bill of materials and reserves the excess
    quantity (all or nothing).

    "Excess quantity" means: only what goes beyond the lines of the document. The document
    lines themselves were already reserved with the order confirmation; booking them again
    here would bind the same stock twice.
    """
    documentNumber: str = Field(description="Number of the order confirmation from the draft.")
    components: list[ComponentLineCreate] = Field(
        description="The bill of materials of the new asset — taken over from the document lines by engineering and possibly adjusted. There is no standard BOM the backend could copy instead."
    )
    note: str | None = Field(
        default=None, description="Free text on the confirmation, as on an ordinary release."
    )


class SparePartsDraft(BaseModel):
    """One entry of the open spare-parts drafts (GET /api/assets/drafts/spare-parts).

    One row per order confirmation with `assetPurpose == 'spareParts'` that is not yet
    assigned to an existing `AssetInstance` through a `SPARE_PART_FOR` edge.
    """
    documentNumber: str = Field(
        description="Number of the order confirmation, for instance 'OC-2026-0003'."
    )
    customer: AssetCustomer | None = Field(
        default=None, description="Customer of the order confirmation."
    )
    date: Neo4jDate | None = Field(default=None, description="Document date, for the ordering.")


class SparePartsLink(InputModel):
    """Schema for linking a spare-parts document with existing assets (POST
    `/api/assets/drafts/spare-parts/{documentNumber}/link`).

    Pure traceability — unlike with an asset draft, no new `AssetInstance` and no
    reservation come about, only the edge `SPARE_PART_FOR`.
    """
    serialNumbers: list[str] = Field(
        min_length=1,
        description="Serial numbers of the existing assets this document delivers spare parts for."
    )


class SparePartsLinkCreated(BaseModel):
    """Answer to `POST /api/assets/drafts/spare-parts/{documentNumber}/link`."""
    documentNumber: str = Field(description="Number of the linked document.")
    serialNumbers: list[str] = Field(description="Serial numbers of the linked assets.")
