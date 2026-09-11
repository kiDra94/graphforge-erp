// ============================================================================
// ACME CORP — DEMO GRAPH SEED
//
// Builds the complete Neo4j graph this backend expects: constraints, indexes,
// master data, transactional data and the structures derived from them.
//
// Everything is generic demo data. Names, numbers, prices and addresses are
// invented; the file is self-contained and reads no external CSV, so a single
//
//     cat seed.cypher | cypher-shell -u neo4j -p <password>
//
// produces a fully populated database on any machine.
//
// CONVENTIONS
//   Money      Integer cents, property suffix "Cent". round() before
//              toInteger(), otherwise values ending in .x5 drift by one cent.
//   Labels     Every Product carries exactly one of :Assembly or :Part,
//              derived from the CONTAINS structure in section 13.
//   Stock      Four movement types, quantity always positive, direction
//              encoded in the type. StockLevel.quantity is a cache rebuilt
//              from the movement history — never written directly.
//   Idempotent Every write is a MERGE on a business key. Running the file
//              twice yields the same graph, not duplicates.
//
// To start from an empty database, uncomment the next line.
// MATCH (n) DETACH DELETE n;
// ============================================================================


// ----------------------------------------------------------------------------
// 1) CONSTRAINTS AND INDEXES
// ----------------------------------------------------------------------------
CREATE CONSTRAINT product_number      IF NOT EXISTS FOR (p:Product)           REQUIRE p.number IS UNIQUE;
CREATE CONSTRAINT category_id         IF NOT EXISTS FOR (c:Category)          REQUIRE c.id IS UNIQUE;
CREATE CONSTRAINT productgroup_id     IF NOT EXISTS FOR (g:ProductGroup)      REQUIRE g.id IS UNIQUE;
CREATE CONSTRAINT subcategory_id      IF NOT EXISTS FOR (s:Subcategory)       REQUIRE s.id IS UNIQUE;
CREATE CONSTRAINT customer_id         IF NOT EXISTS FOR (c:Customer)          REQUIRE c.id IS UNIQUE;
CREATE CONSTRAINT supplier_id         IF NOT EXISTS FOR (s:Supplier)          REQUIRE s.id IS UNIQUE;
CREATE CONSTRAINT employee_id         IF NOT EXISTS FOR (e:Employee)          REQUIRE e.id IS UNIQUE;
CREATE CONSTRAINT role_name           IF NOT EXISTS FOR (r:Role)              REQUIRE r.name IS UNIQUE;
CREATE CONSTRAINT location_id         IF NOT EXISTS FOR (l:Location)          REQUIRE l.id IS UNIQUE;
CREATE CONSTRAINT stocklevel_id       IF NOT EXISTS FOR (s:StockLevel)        REQUIRE s.id IS UNIQUE;
CREATE CONSTRAINT stockmovement_id    IF NOT EXISTS FOR (m:StockMovement)     REQUIRE m.id IS UNIQUE;
CREATE CONSTRAINT document_number     IF NOT EXISTS FOR (d:Document)          REQUIRE d.number IS UNIQUE;
CREATE CONSTRAINT documentline_id     IF NOT EXISTS FOR (l:DocumentLine)      REQUIRE l.id IS UNIQUE;
CREATE CONSTRAINT order_projectnumber IF NOT EXISTS FOR (o:Order)             REQUIRE o.projectNumber IS UNIQUE;
CREATE CONSTRAINT orderline_id        IF NOT EXISTS FOR (l:OrderLine)         REQUIRE l.id IS UNIQUE;
CREATE CONSTRAINT asset_serialnumber  IF NOT EXISTS FOR (a:AssetInstance)     REQUIRE a.serialNumber IS UNIQUE;
CREATE CONSTRAINT asset_internal      IF NOT EXISTS FOR (a:AssetInstance)     REQUIRE a.internalNumber IS UNIQUE;
CREATE CONSTRAINT component_id        IF NOT EXISTS FOR (c:ComponentInstance) REQUIRE c.id IS UNIQUE;
CREATE CONSTRAINT contract_id         IF NOT EXISTS FOR (c:Contract)          REQUIRE c.id IS UNIQUE;
CREATE CONSTRAINT discount_id         IF NOT EXISTS FOR (d:Discount)          REQUIRE d.id IS UNIQUE;

// The e-mail address and the initials are both login keys: POST /api/auth/login
// takes a single `identifier` field and looks it up as initials when it contains
// no '@'. Two employees sharing either value would make the login ambiguous.
// Employees without initials stay allowed — uniqueness constraints ignore
// missing properties — and sign in with their e-mail address.
CREATE CONSTRAINT employee_email      IF NOT EXISTS FOR (e:Employee)          REQUIRE e.email IS UNIQUE;
CREATE CONSTRAINT employee_initials   IF NOT EXISTS FOR (e:Employee)          REQUIRE e.initials IS UNIQUE;

// A goods receipt is identified by the purchase order it settles plus the
// supplier's delivery note number. The pair guards against booking the same
// delivery note twice. Neo4j Community has no node key constraint, but a
// composite uniqueness constraint over two properties works here as well.
CREATE CONSTRAINT goodsreceipt_note   IF NOT EXISTS FOR (r:GoodsReceipt)
  REQUIRE (r.purchaseOrderNumber, r.deliveryNoteNumber) IS UNIQUE;

// One ServiceEvent per component, id = the ComponentInstance id. It holds only
// the latest completion, not a history: completedAt doubles as the new anchor
// for the next service cycle.
CREATE CONSTRAINT serviceevent_id     IF NOT EXISTS FOR (s:ServiceEvent)      REQUIRE s.id IS UNIQUE;

// Notification ids are business keys (service_{componentId}_{year},
// shortage_{receiptNumber}_{lineNumber}), which keeps every creation path
// MERGE-idempotent.
CREATE CONSTRAINT notification_id     IF NOT EXISTS FOR (n:Notification)      REQUIRE n.id IS UNIQUE;

// A supplier used to carry a second, human-readable number beside its id. Once the
// counter behind it became a uuid, the field said nothing the id did not already say and
// was removed. The DROP matters for a database that was seeded before that: a constraint
// left behind would keep guarding a property nothing writes any more.
DROP CONSTRAINT supplier_number IF EXISTS;

CREATE INDEX product_label     IF NOT EXISTS FOR (p:Product)       ON (p.label);
CREATE INDEX document_type     IF NOT EXISTS FOR (d:Document)      ON (d.type);
CREATE INDEX movement_type     IF NOT EXISTS FOR (m:StockMovement) ON (m.type);
CREATE INDEX assembly_number   IF NOT EXISTS FOR (a:Assembly)      ON (a.number);


// ----------------------------------------------------------------------------
// 2) PRODUCT GROUP HIERARCHY
// Category -> ProductGroup -> Subcategory -> Product, three levels, each node
// pointing upwards at its parent.
// ----------------------------------------------------------------------------
UNWIND [
  {id: 1, name: 'Hardware'},
  {id: 2, name: 'Services'}
] AS row
MERGE (c:Category {id: row.id})
SET c.name = row.name;

UNWIND [
  {id: 10, name: 'Components',     categoryId: 1},
  {id: 11, name: 'Systems',        categoryId: 1},
  {id: 20, name: 'Field Services', categoryId: 2}
] AS row
MERGE (g:ProductGroup {id: row.id})
SET g.name = row.name
WITH g, row
MATCH (c:Category {id: row.categoryId})
MERGE (g)-[:BELONGS_TO_CATEGORY]->(c);

UNWIND [
  {id: 100, name: 'Fasteners',    groupId: 10},
  {id: 101, name: 'Seals',        groupId: 10},
  {id: 102, name: 'Electronics',  groupId: 10},
  {id: 110, name: 'Starter Kits', groupId: 11},
  {id: 200, name: 'Installation', groupId: 20}
] AS row
MERGE (s:Subcategory {id: row.id})
SET s.name = row.name
WITH s, row
MATCH (g:ProductGroup {id: row.groupId})
MERGE (s)-[:PART_OF]->(g);


// ----------------------------------------------------------------------------
// 3) PRODUCTS
//
// stockEffect steers what a document line does to the warehouse:
//   'direct'          ordinary stock item, booked one to one
//   'billOfMaterials' sold as an assembly, its components are booked instead
//                     and an AssetInstance is created on order confirmation
//   'none'            labour and flat fees, no stock movement at all
// ----------------------------------------------------------------------------
UNWIND [
  {number: 'ACME-1000', label: 'Starter Kit A',       shortText: 'Kit A',        shortTextEn: 'Kit A',
   description: 'Complete starter kit, first generation.', unit: 'pcs', subcategoryId: 110,
   listPriceCent: 129900, costPriceCent: 74500, laborRateCent: null, taxPercent: 20.0,
   grossWeightKg: 12.4, netWeightKg: 11.8, gtin: '4000000010007', manufacturerNumber: 'KIT-A-G1',
   commodityCode: '84139100', countryOfOrigin: 'AT', discountable: true,
   minStock: 2, targetStock: 6, stockEffect: 'billOfMaterials', usesSerialNumbers: true},

  {number: 'ACME-1001', label: 'Starter Kit B',       shortText: 'Kit B',        shortTextEn: 'Kit B',
   description: 'Complete starter kit, second generation with control board.', unit: 'pcs', subcategoryId: 110,
   listPriceCent: 149900, costPriceCent: 86000, laborRateCent: null, taxPercent: 20.0,
   grossWeightKg: 13.1, netWeightKg: 12.5, gtin: '4000000010014', manufacturerNumber: 'KIT-B-G2',
   commodityCode: '84139100', countryOfOrigin: 'AT', discountable: true,
   minStock: 2, targetStock: 8, stockEffect: 'billOfMaterials', usesSerialNumbers: true},

  {number: 'ACME-2001', label: 'Screw M6x20',         shortText: 'Screw M6x20',  shortTextEn: 'Screw M6x20',
   description: 'Socket head cap screw M6x20, zinc plated.', unit: 'pcs', subcategoryId: 100,
   listPriceCent: 15, costPriceCent: 4, laborRateCent: null, taxPercent: 20.0,
   grossWeightKg: 0.01, netWeightKg: 0.01, gtin: '4000000020014', manufacturerNumber: 'SCR-M6-20',
   commodityCode: '73181562', countryOfOrigin: 'DE', discountable: true,
   minStock: 200, targetStock: 1000, stockEffect: 'direct', usesSerialNumbers: false},

  {number: 'ACME-2002', label: 'O-Ring 10x2',         shortText: 'O-Ring 10x2',  shortTextEn: 'O-Ring 10x2',
   description: 'O-ring 10x2 mm, NBR 70 Shore A.', unit: 'pcs', subcategoryId: 101,
   listPriceCent: 20, costPriceCent: 6, laborRateCent: null, taxPercent: 20.0,
   grossWeightKg: 0.002, netWeightKg: 0.002, gtin: '4000000020021', manufacturerNumber: 'OR-10X2',
   commodityCode: '40169300', countryOfOrigin: 'DE', discountable: true,
   minStock: 100, targetStock: 500, stockEffect: 'direct', usesSerialNumbers: false},

  {number: 'ACME-2003', label: 'Filter Cartridge',    shortText: 'Filter',       shortTextEn: 'Filter',
   description: 'Replaceable filter cartridge, standard mesh.', unit: 'pcs', subcategoryId: 101,
   listPriceCent: 2450, costPriceCent: 1100, laborRateCent: null, taxPercent: 20.0,
   grossWeightKg: 0.35, netWeightKg: 0.30, gtin: '4000000020038', manufacturerNumber: 'FLT-STD',
   commodityCode: '84212300', countryOfOrigin: 'AT', discountable: true,
   minStock: 20, targetStock: 80, stockEffect: 'direct', usesSerialNumbers: false},

  {number: 'ACME-2004', label: 'Pump Unit',           shortText: 'Pump',         shortTextEn: 'Pump',
   description: 'Pump unit, pre-assembled.', unit: 'pcs', subcategoryId: 110,
   listPriceCent: 48900, costPriceCent: 27500, laborRateCent: null, taxPercent: 20.0,
   grossWeightKg: 5.2, netWeightKg: 4.9, gtin: '4000000020045', manufacturerNumber: 'PMP-01',
   commodityCode: '84136031', countryOfOrigin: 'AT', discountable: true,
   minStock: 3, targetStock: 10, stockEffect: 'direct', usesSerialNumbers: false},

  {number: 'ACME-2005', label: 'Hose 2m',             shortText: 'Hose 2m',      shortTextEn: 'Hose 2m',
   description: 'Flexible hose, 2 m, with fittings.', unit: 'pcs', subcategoryId: 100,
   listPriceCent: 1890, costPriceCent: 820, laborRateCent: null, taxPercent: 20.0,
   grossWeightKg: 0.6, netWeightKg: 0.55, gtin: '4000000020052', manufacturerNumber: 'HOS-2000',
   commodityCode: '40093100', countryOfOrigin: 'IT', discountable: true,
   minStock: 30, targetStock: 120, stockEffect: 'direct', usesSerialNumbers: false},

  {number: 'ACME-2006', label: 'Control Board',       shortText: 'Board',        shortTextEn: 'Board',
   description: 'Control board with display.', unit: 'pcs', subcategoryId: 102,
   listPriceCent: 34900, costPriceCent: 19800, laborRateCent: null, taxPercent: 20.0,
   grossWeightKg: 0.4, netWeightKg: 0.35, gtin: '4000000020069', manufacturerNumber: 'CTL-D1',
   commodityCode: '85371091', countryOfOrigin: 'DE', discountable: false,
   minStock: 5, targetStock: 15, stockEffect: 'direct', usesSerialNumbers: false},

  {number: 'ACME-2007', label: 'Vibration Sensor',    shortText: 'Sensor',       shortTextEn: 'Sensor',
   description: 'Vibration sensor with M12 connector.', unit: 'pcs', subcategoryId: 102,
   listPriceCent: 12900, costPriceCent: 6400, laborRateCent: null, taxPercent: 20.0,
   grossWeightKg: 0.15, netWeightKg: 0.12, gtin: '4000000020076', manufacturerNumber: 'SNS-V1',
   commodityCode: '90312000', countryOfOrigin: 'DE', discountable: true,
   minStock: 10, targetStock: 40, stockEffect: 'direct', usesSerialNumbers: false},

  {number: 'ACME-2008', label: 'Mounting Bracket',    shortText: 'Bracket',      shortTextEn: 'Bracket',
   description: 'Steel mounting bracket, powder coated.', unit: 'pcs', subcategoryId: 100,
   listPriceCent: 640, costPriceCent: 240, laborRateCent: null, taxPercent: 20.0,
   grossWeightKg: 0.8, netWeightKg: 0.78, gtin: '4000000020083', manufacturerNumber: 'BRK-01',
   commodityCode: '73269098', countryOfOrigin: 'AT', discountable: true,
   minStock: 40, targetStock: 160, stockEffect: 'direct', usesSerialNumbers: false},

  {number: 'ACME-2009', label: 'Filter Cartridge XL', shortText: 'Filter XL',    shortTextEn: 'Filter XL',
   description: 'Replaceable filter cartridge, fine mesh.', unit: 'pcs', subcategoryId: 101,
   listPriceCent: 2890, costPriceCent: 1290, laborRateCent: null, taxPercent: 20.0,
   grossWeightKg: 0.38, netWeightKg: 0.33, gtin: '4000000020090', manufacturerNumber: 'FLT-XL',
   commodityCode: '84212300', countryOfOrigin: 'AT', discountable: true,
   minStock: 20, targetStock: 80, stockEffect: 'direct', usesSerialNumbers: false},

  // Labour and flat fees. They carry a labour rate instead of a list price and
  // never touch the warehouse — hence stockEffect 'none'.
  {number: 'ACME-3001', label: 'Installation',        shortText: 'Installation', shortTextEn: 'Installation',
   description: 'On-site installation, per hour.', unit: 'h', subcategoryId: 200,
   listPriceCent: 0, costPriceCent: null, laborRateCent: 9500, taxPercent: 20.0,
   grossWeightKg: null, netWeightKg: null, gtin: null, manufacturerNumber: null,
   commodityCode: null, countryOfOrigin: null, discountable: false,
   minStock: null, targetStock: null, stockEffect: 'none', usesSerialNumbers: false},

  {number: 'ACME-3002', label: 'Commissioning',       shortText: 'Commissioning', shortTextEn: 'Commissioning',
   description: 'Commissioning and handover, flat fee.', unit: 'ea', subcategoryId: 200,
   listPriceCent: 45000, costPriceCent: null, laborRateCent: null, taxPercent: 20.0,
   grossWeightKg: null, netWeightKg: null, gtin: null, manufacturerNumber: null,
   commodityCode: null, countryOfOrigin: null, discountable: false,
   minStock: null, targetStock: null, stockEffect: 'none', usesSerialNumbers: false}
] AS row
MERGE (p:Product {number: row.number})
SET p.label               = row.label,
    p.shortText           = row.shortText,
    p.shortTextEn         = row.shortTextEn,
    p.description         = row.description,
    p.unit                = row.unit,
    p.listPriceCent       = row.listPriceCent,
    p.costPriceCent       = row.costPriceCent,
    p.laborRateCent       = row.laborRateCent,
    p.taxPercent          = row.taxPercent,
    p.grossWeightKg       = row.grossWeightKg,
    p.netWeightKg         = row.netWeightKg,
    p.gtin                = row.gtin,
    p.manufacturerNumber  = row.manufacturerNumber,
    p.commodityCode       = row.commodityCode,
    p.countryOfOrigin     = row.countryOfOrigin,
    // Steers whether a discount may be granted on this product at all.
    p.discountable        = row.discountable,
    p.minStock            = row.minStock,
    p.targetStock         = row.targetStock,
    p.stockEffect         = row.stockEffect,
    p.usesSerialNumbers   = row.usesSerialNumbers,
    p.active              = true,
    // Assembly behaviour. Defaults here, overridden for real assemblies below.
    p.assemblyDeductsComponents  = false,
    p.assemblyPrintsComponents   = false,
    p.assemblyPriceFromComponents = false,
    p.isWearPart          = false
WITH p, row
MATCH (s:Subcategory {id: row.subcategoryId})
MERGE (p)-[:BELONGS_TO]->(s);

// The two kits are sold as assemblies: their components are deducted from stock
// and printed on the delivery note, the price comes from the kit itself.
MATCH (p:Product) WHERE p.number IN ['ACME-1000', 'ACME-1001']
SET p.assemblyDeductsComponents   = true,
    p.assemblyPrintsComponents    = true,
    p.assemblyPriceFromComponents = false;


// ----------------------------------------------------------------------------
// 4) WEAR PARTS AND SERVICE INTERVAL
//
// A wear part is replaced on a fixed schedule. The interval counts from the day
// the asset shipped, not from the day the part was installed — see
// AssetServiceRepository.get_due_services.
//
// The three intervals are deliberately different, so that one unit shipped on
// 2026-03-18 produces all three cases at once in GET /api/assets/service: the
// three-month part is overdue, the six-month part falls due inside the
// thirty-day window, and the twelve-month part stays out of it.
// ----------------------------------------------------------------------------
MATCH (p:Product) WHERE p.number IN ['ACME-2002', 'ACME-2009']
SET p.isWearPart = true, p.serviceIntervalMonths = 12;

MATCH (p:Product {number: 'ACME-2003'})
SET p.isWearPart = true, p.serviceIntervalMonths = 6;

MATCH (p:Product {number: 'ACME-2007'})
SET p.isWearPart = true, p.serviceIntervalMonths = 3;


// ----------------------------------------------------------------------------
// 5) BILL OF MATERIALS
//
// ACME-2004 is itself an assembly inside the kits, so the structure is two
// levels deep. That is deliberate: the digital twin derivation in section 13
// only resolves leaves, and a nested assembly proves it walks the whole tree.
// ----------------------------------------------------------------------------
UNWIND [
  {parent: 'ACME-1000', child: 'ACME-2004', quantity: 1.0,  unit: 'pcs'},
  {parent: 'ACME-1000', child: 'ACME-2005', quantity: 2.0,  unit: 'pcs'},
  {parent: 'ACME-1000', child: 'ACME-2008', quantity: 4.0,  unit: 'pcs'},
  {parent: 'ACME-1000', child: 'ACME-2001', quantity: 12.0, unit: 'pcs'},

  {parent: 'ACME-1001', child: 'ACME-2004', quantity: 1.0,  unit: 'pcs'},
  {parent: 'ACME-1001', child: 'ACME-2005', quantity: 2.0,  unit: 'pcs'},
  {parent: 'ACME-1001', child: 'ACME-2008', quantity: 4.0,  unit: 'pcs'},
  {parent: 'ACME-1001', child: 'ACME-2001', quantity: 16.0, unit: 'pcs'},
  {parent: 'ACME-1001', child: 'ACME-2006', quantity: 1.0,  unit: 'pcs'},

  {parent: 'ACME-2004', child: 'ACME-2002', quantity: 2.0,  unit: 'pcs'},
  {parent: 'ACME-2004', child: 'ACME-2003', quantity: 1.0,  unit: 'pcs'},
  {parent: 'ACME-2004', child: 'ACME-2007', quantity: 1.0,  unit: 'pcs'}
] AS row
MATCH (parent:Product {number: row.parent})
MATCH (child:Product  {number: row.child})
MERGE (parent)-[r:CONTAINS]->(child)
SET r.quantity = row.quantity,
    r.unit     = row.unit;


// ----------------------------------------------------------------------------
// 6) ROLES, EMPLOYEES, CUSTOMERS
//
// Every demo account uses the password "demo1234". The stored value is a bcrypt
// hash including its salt — demo credentials, never reuse them anywhere real.
// ----------------------------------------------------------------------------
UNWIND ['User', 'Sales', 'Purchasing', 'Engineering', 'BackOffice', 'Accounting', 'Warehouse', 'Admin'] AS roleName
MERGE (r:Role {name: roleName});

UNWIND [
  {id: '1', name: 'Max Mustermann', email: 'max.mustermann@acme.example',  initials: 'MM', roles: ['Sales', 'User']},
  {id: '2', name: 'Erika Musterfrau', email: 'erika.musterfrau@acme.example', initials: 'EM', roles: ['Engineering', 'User']},
  {id: '3', name: 'John Doe',       email: 'john.doe@acme.example',        initials: 'JD', roles: ['Purchasing', 'User']},
  {id: '4', name: 'Jane Roe',       email: 'jane.roe@acme.example',        initials: 'JR', roles: ['BackOffice', 'Accounting', 'User']},
  {id: '5', name: 'Sam Sample',     email: 'sam.sample@acme.example',      initials: 'SS', roles: ['Warehouse', 'User']},
  {id: '6', name: 'Alex Admin',     email: 'alex.admin@acme.example',      initials: 'AA', roles: ['Admin']}
] AS row
MERGE (e:Employee {id: row.id})
SET e.name     = row.name,
    e.email    = row.email,
    e.initials = row.initials,
    e.active   = true,
    e.password = '$2b$12$L9e0wVUjDql5.XTKVB6dU.SyqbC2UrseT0JLuUD6pdHS1OzARIga6'
WITH e, row
UNWIND row.roles AS roleName
MATCH (r:Role {name: roleName})
MERGE (e)-[:HAS_ROLE]->(r);

UNWIND [
  {id: 'C-1001', name: 'Example Industries GmbH', street: 'Musterstrasse 1',  city: '1010 Vienna',  country: 'AT', vatId: 'ATU00000001', language: 'DE', accountManagerId: '1'},
  {id: 'C-1002', name: 'Sample Logistics AG',     street: 'Beispielweg 12',   city: '10115 Berlin', country: 'DE', vatId: 'DE000000002',  language: 'DE', accountManagerId: '1'},
  {id: 'C-1003', name: 'Demo Retail Ltd',         street: '5 Sample Street',  city: 'London EC1',   country: 'GB', vatId: 'GB000000003',  language: 'EN', accountManagerId: '4'}
] AS row
MERGE (c:Customer {id: row.id})
SET c.name     = row.name,
    c.street   = row.street,
    c.city     = row.city,
    c.country  = row.country,
    c.vatId    = row.vatId,
    c.language = row.language
WITH c, row
MATCH (e:Employee {id: row.accountManagerId})
// Direction employee -> customer, the way the service reminder query expects it.
MERGE (e)-[:ACCOUNT_MANAGER_OF]->(c);


// ----------------------------------------------------------------------------
// 7) SUPPLIERS AND SUPPLY CONDITIONS
//
// Every product is offered by all three suppliers so the reorder analysis has a
// choice to make. Exactly one of them carries isPreferredSupplier = true.
// ----------------------------------------------------------------------------
UNWIND [
  {id: 'S-001', name: 'Northern Supply GmbH',  city: 'Hamburg',  country: 'DE', email: 'sales@northern-supply.example', phone: '+49 40 1000001', street: 'Hafenstrasse 1',  vatId: 'DE100000001'},
  {id: 'S-002', name: 'Southern Parts AG',     city: 'Munich',   country: 'DE', email: 'sales@southern-parts.example',  phone: '+49 89 1000002', street: 'Werkstrasse 12',  vatId: 'DE100000002'},
  {id: 'S-003', name: 'Global Components Ltd', city: 'Dublin',   country: 'IE', email: 'sales@global-components.example', phone: '+353 1 1000003', street: 'Dock Road 8',   vatId: 'IE1000003X'}
] AS row
MERGE (s:Supplier {id: row.id})
SET s.name           = row.name,
    s.city           = row.city,
    s.country        = row.country,
    s.email          = row.email,
    s.phone          = row.phone,
    s.street         = row.street,
    s.vatId          = row.vatId;

// Purchase prices derive from the product's own cost price with a fixed factor
// per supplier, so the data stays consistent when products are added. S-001 is
// always the cheapest and the preferred source.
MATCH (p:Product) WHERE p.costPriceCent IS NOT NULL
UNWIND [
  {supplierId: 'S-001', factor: 1.00, leadTimeDays: 5,  preferred: true},
  {supplierId: 'S-002', factor: 1.08, leadTimeDays: 3,  preferred: false},
  {supplierId: 'S-003', factor: 1.15, leadTimeDays: 12, preferred: false}
] AS row
MATCH (s:Supplier {id: row.supplierId})
MERGE (s)-[r:SUPPLIES_PRODUCT]->(p)
SET r.purchasePriceCent   = toInteger(round(p.costPriceCent * row.factor)),
    r.leadTimeDays        = row.leadTimeDays,
    r.isPreferredSupplier = row.preferred;


// ----------------------------------------------------------------------------
// 8) CONTRACTS AND DISCOUNTS
//
// Two axes exist side by side: a Contract grants a flat percentage to the
// customers linked to it (or to everyone, when isGlobal), while a CONDITION_FOR
// edge pins a fixed price for a single product. Discount nodes cover the
// time-boxed group and product campaigns.
//
// All percentages in this demo are a flat 10 %.
// ----------------------------------------------------------------------------
UNWIND [
  {id: 'CTR-001', name: 'Standard Framework Agreement', description: 'Applies to every customer.',
   validFrom: '2026-01-01', validTo: '2026-12-31', isGlobal: true,  discountPercent: 10.0, customerId: null},
  {id: 'CTR-002', name: 'Key Account Agreement',       description: 'Negotiated agreement for a single customer.',
   validFrom: '2026-01-01', validTo: '2027-12-31', isGlobal: false, discountPercent: 10.0, customerId: 'C-1001'}
] AS row
MERGE (c:Contract {id: row.id})
SET c.name            = row.name,
    c.description     = row.description,
    c.validFrom       = date(row.validFrom),
    c.validTo         = date(row.validTo),
    c.isGlobal        = row.isGlobal,
    c.discountPercent = row.discountPercent
WITH c, row
WHERE row.customerId IS NOT NULL
MATCH (customer:Customer {id: row.customerId})
MERGE (customer)-[:HAS_CONTRACT]->(c);

UNWIND [
  {contractId: 'CTR-002', productNumber: 'ACME-2003', fixedPriceCent: 1990},
  {contractId: 'CTR-002', productNumber: 'ACME-1000', fixedPriceCent: 109900}
] AS row
MATCH (c:Contract {id: row.contractId})
MATCH (p:Product  {number: row.productNumber})
MERGE (c)-[k:CONDITION_FOR]->(p)
SET k.fixedPriceCent = row.fixedPriceCent;

UNWIND [
  {id: 1, type: 'GroupDiscount',   value: 10.0, validFrom: '2026-01-01', validTo: '2026-12-31', productGroupId: 10,   productNumber: null},
  {id: 2, type: 'ProductDiscount', value: 10.0, validFrom: '2026-01-01', validTo: '2026-12-31', productGroupId: null, productNumber: 'ACME-2005'}
] AS row
MERGE (d:Discount {id: row.id})
SET d.type      = row.type,
    // value is a percentage and is deliberately NOT stored in cents.
    d.value     = row.value,
    d.validFrom = date(row.validFrom),
    d.validTo   = date(row.validTo)
WITH d, row
FOREACH (_ IN CASE WHEN row.productGroupId IS NOT NULL THEN [1] ELSE [] END |
  MERGE (g:ProductGroup {id: row.productGroupId})
  MERGE (d)-[:APPLIES_TO_GROUP]->(g)
)
FOREACH (_ IN CASE WHEN row.productNumber IS NOT NULL THEN [1] ELSE [] END |
  MERGE (p:Product {number: row.productNumber})
  MERGE (d)-[:APPLIES_TO_PRODUCT]->(p)
);


// ----------------------------------------------------------------------------
// 9) WAREHOUSE LOCATIONS
// ----------------------------------------------------------------------------
UNWIND [
  {id: '1', name: 'Central Warehouse', type: 'Warehouse'},
  {id: '2', name: 'Service Van 1',     type: 'Vehicle'},
  {id: '3', name: 'Service Van 2',     type: 'Vehicle'}
] AS row
MERGE (l:Location {id: row.id})
SET l.name = row.name, l.type = row.type;


// ----------------------------------------------------------------------------
// 10) ORDERS AND DOCUMENTS
//
// The Order is the bracket over a whole chain: quote, order confirmation,
// delivery note and invoice all carry the same project number. Purchasing
// documents stay outside — they hang off the supplier instead.
// ----------------------------------------------------------------------------
UNWIND [
  {projectNumber: '2026-0001', customerId: 'C-1001', deliveryDate: '2026-03-20', year: 2026},
  {projectNumber: '2026-0002', customerId: 'C-1002', deliveryDate: '2026-05-15', year: 2026}
] AS row
MATCH (c:Customer {id: row.customerId})
MERGE (o:Order {projectNumber: row.projectNumber})
SET o.deliveryDate = date(row.deliveryDate),
    o.year         = row.year
MERGE (c)-[:HAS_ORDER]->(o);

UNWIND [
  // Sales chain for order 2026-0001, fully settled.
  {number: 'QU-2026-0001', type: 'Quote',             date: '2026-01-15', status: 'completed', language: 'DE',
   customerId: 'C-1001', supplierId: null, employeeId: '1', basedOn: null,             projectNumber: '2026-0001',
   orderDiscountPercent: 0.0,  taxPercent: 20.0, assetPurpose: 'newAsset', purchaseOrderNumber: null, deliveryNoteNumber: null},
  {number: 'OC-2026-0001', type: 'OrderConfirmation', date: '2026-01-22', status: 'completed', language: 'DE',
   customerId: 'C-1001', supplierId: null, employeeId: '1', basedOn: 'QU-2026-0001', projectNumber: '2026-0001',
   orderDiscountPercent: 5.0,  taxPercent: 20.0, assetPurpose: 'newAsset', purchaseOrderNumber: null, deliveryNoteNumber: null},
  {number: 'DN-2026-0001', type: 'DeliveryNote',      date: '2026-03-18', status: 'completed', language: 'DE',
   customerId: 'C-1001', supplierId: null, employeeId: '5', basedOn: 'OC-2026-0001', projectNumber: '2026-0001',
   orderDiscountPercent: 0.0,  taxPercent: 20.0, assetPurpose: 'newAsset', purchaseOrderNumber: null, deliveryNoteNumber: null},
  {number: 'IN-2026-0001', type: 'Invoice',           date: '2026-03-19', status: 'completed', language: 'DE',
   customerId: 'C-1001', supplierId: null, employeeId: '4', basedOn: 'DN-2026-0001', projectNumber: '2026-0001',
   orderDiscountPercent: 5.0,  taxPercent: 20.0, assetPurpose: 'newAsset', purchaseOrderNumber: null, deliveryNoteNumber: null},

  // Second sales chain, still open — gives the UI something in progress.
  {number: 'QU-2026-0002', type: 'Quote',             date: '2026-04-02', status: 'open',      language: 'DE',
   customerId: 'C-1002', supplierId: null, employeeId: '1', basedOn: null,             projectNumber: '2026-0002',
   orderDiscountPercent: 0.0,  taxPercent: 20.0, assetPurpose: 'newAsset', purchaseOrderNumber: null, deliveryNoteNumber: null},
  {number: 'OC-2026-0002', type: 'OrderConfirmation', date: '2026-04-10', status: 'open',      language: 'DE',
   customerId: 'C-1002', supplierId: null, employeeId: '1', basedOn: 'QU-2026-0002', projectNumber: '2026-0002',
   orderDiscountPercent: 10.0, taxPercent: 20.0, assetPurpose: 'newAsset', purchaseOrderNumber: null, deliveryNoteNumber: null},

  // An English-language quote, so the shortTextEn path is exercised.
  {number: 'QU-2026-0003', type: 'Quote',             date: '2026-04-14', status: 'open',      language: 'EN',
   customerId: 'C-1003', supplierId: null, employeeId: '4', basedOn: null,             projectNumber: null,
   orderDiscountPercent: 0.0,  taxPercent: 20.0, assetPurpose: 'spareParts', purchaseOrderNumber: null, deliveryNoteNumber: null},

  // Purchasing chain.
  {number: 'PO-2026-0001', type: 'PurchaseOrder',     date: '2026-02-05', status: 'completed', language: 'DE',
   customerId: null, supplierId: 'S-001', employeeId: '3', basedOn: null,             projectNumber: null,
   orderDiscountPercent: 0.0,  taxPercent: 20.0, assetPurpose: null,       purchaseOrderNumber: null, deliveryNoteNumber: null},
  {number: 'GR-2026-0001', type: 'GoodsReceipt',      date: '2026-02-12', status: 'completed', language: 'DE',
   customerId: null, supplierId: 'S-001', employeeId: '5', basedOn: 'PO-2026-0001', projectNumber: null,
   orderDiscountPercent: 0.0,  taxPercent: 20.0, assetPurpose: null,       purchaseOrderNumber: 'PO-2026-0001', deliveryNoteNumber: 'DN-NS-99871'}
] AS row
MERGE (d:Document {number: row.number})
SET d.type                 = row.type,
    d.date                 = date(row.date),
    d.status               = row.status,
    d.language             = row.language,
    d.orderDiscountPercent = row.orderDiscountPercent,
    d.taxPercent           = row.taxPercent,
    d.assetPurpose         = row.assetPurpose,
    d.purchaseOrderNumber  = row.purchaseOrderNumber,
    d.deliveryNoteNumber   = row.deliveryNoteNumber
WITH d, row
MATCH (e:Employee {id: row.employeeId})
MERGE (d)-[:CREATED_BY]->(e);

// The business partner in its own pass, so both sides can use MATCH. Sales
// documents hang off a customer, purchasing documents off a supplier — never
// both. A FOREACH here would have forced MERGE on the partner and silently
// created an empty node from a typo.
UNWIND [
  {number: 'QU-2026-0001', customerId: 'C-1001'},
  {number: 'OC-2026-0001', customerId: 'C-1001'},
  {number: 'DN-2026-0001', customerId: 'C-1001'},
  {number: 'IN-2026-0001', customerId: 'C-1001'},
  {number: 'QU-2026-0002', customerId: 'C-1002'},
  {number: 'OC-2026-0002', customerId: 'C-1002'},
  {number: 'QU-2026-0003', customerId: 'C-1003'}
] AS row
MATCH (d:Document {number: row.number})
MATCH (c:Customer {id: row.customerId})
MERGE (d)-[:BELONGS_TO_CUSTOMER]->(c);

UNWIND [
  {number: 'PO-2026-0001', supplierId: 'S-001'},
  {number: 'GR-2026-0001', supplierId: 'S-001'}
] AS row
MATCH (d:Document {number: row.number})
MATCH (s:Supplier {id: row.supplierId})
MERGE (d)-[:BELONGS_TO_SUPPLIER]->(s);

// The document chain. A separate pass so both sides can use MATCH: a wrong
// predecessor number should fail loudly instead of creating an empty document.
UNWIND [
  {number: 'OC-2026-0001', basedOn: 'QU-2026-0001'},
  {number: 'DN-2026-0001', basedOn: 'OC-2026-0001'},
  {number: 'IN-2026-0001', basedOn: 'DN-2026-0001'},
  {number: 'OC-2026-0002', basedOn: 'QU-2026-0002'},
  {number: 'GR-2026-0001', basedOn: 'PO-2026-0001'}
] AS row
MATCH (d:Document         {number: row.number})
MATCH (p:Document {number: row.basedOn})
MERGE (d)-[:BASED_ON]->(p);

// Same reasoning for the order link.
UNWIND [
  {number: 'QU-2026-0001', projectNumber: '2026-0001'},
  {number: 'OC-2026-0001', projectNumber: '2026-0001'},
  {number: 'DN-2026-0001', projectNumber: '2026-0001'},
  {number: 'IN-2026-0001', projectNumber: '2026-0001'},
  {number: 'QU-2026-0002', projectNumber: '2026-0002'},
  {number: 'OC-2026-0002', projectNumber: '2026-0002'}
] AS row
MATCH (d:Document {number: row.number})
MATCH (o:Order    {projectNumber: row.projectNumber})
MERGE (d)-[:BELONGS_TO_ORDER]->(o);

// The document type lives twice: as the `type` property and as a second label
// on the node. The label makes "all invoices" a label scan instead of a
// property filter. Written out per type rather than through APOC, so the file
// runs on a plain Neo4j installation with no plugins.
MATCH (d:Document {type: 'Quote'})             SET d:Quote;
MATCH (d:Document {type: 'OrderConfirmation'}) SET d:OrderConfirmation;
MATCH (d:Document {type: 'DeliveryNote'})      SET d:DeliveryNote;
MATCH (d:Document {type: 'Invoice'})           SET d:Invoice;
MATCH (d:Document {type: 'PurchaseOrder'})     SET d:PurchaseOrder;
MATCH (d:Document {type: 'GoodsReceipt'})      SET d:GoodsReceipt;

// Document lines. hasFixedPrice marks a line whose price came from a contract
// condition; such lines keep their price and are excluded from the basis of the
// order-level discount. `reason` records why a price was typed over by hand.
UNWIND [
  {documentNumber: 'QU-2026-0001', lineNumber: 1, productNumber: 'ACME-1000', quantity: 1.0,  unitPriceCent: 129900, discountPercent: 0.0,  hasFixedPrice: false, priceOverridden: false, reason: null},
  {documentNumber: 'QU-2026-0001', lineNumber: 2, productNumber: 'ACME-3001', quantity: 4.0,  unitPriceCent: 9500,   discountPercent: 0.0,  hasFixedPrice: false, priceOverridden: false, reason: null},

  {documentNumber: 'OC-2026-0001', lineNumber: 1, productNumber: 'ACME-1000', quantity: 1.0,  unitPriceCent: 109900, discountPercent: 0.0,  hasFixedPrice: true,  priceOverridden: false, reason: null},
  {documentNumber: 'OC-2026-0001', lineNumber: 2, productNumber: 'ACME-3001', quantity: 4.0,  unitPriceCent: 9500,   discountPercent: 0.0,  hasFixedPrice: false, priceOverridden: false, reason: null},
  {documentNumber: 'OC-2026-0001', lineNumber: 3, productNumber: 'ACME-3002', quantity: 1.0,  unitPriceCent: 45000,  discountPercent: 0.0,  hasFixedPrice: false, priceOverridden: false, reason: null},

  {documentNumber: 'DN-2026-0001', lineNumber: 1, productNumber: 'ACME-1000', quantity: 1.0,  unitPriceCent: 109900, discountPercent: 0.0,  hasFixedPrice: true,  priceOverridden: false, reason: null},

  {documentNumber: 'IN-2026-0001', lineNumber: 1, productNumber: 'ACME-1000', quantity: 1.0,  unitPriceCent: 109900, discountPercent: 0.0,  hasFixedPrice: true,  priceOverridden: false, reason: null},
  {documentNumber: 'IN-2026-0001', lineNumber: 2, productNumber: 'ACME-3001', quantity: 4.0,  unitPriceCent: 9500,   discountPercent: 0.0,  hasFixedPrice: false, priceOverridden: false, reason: null},
  {documentNumber: 'IN-2026-0001', lineNumber: 3, productNumber: 'ACME-3002', quantity: 1.0,  unitPriceCent: 45000,  discountPercent: 0.0,  hasFixedPrice: false, priceOverridden: false, reason: null},

  {documentNumber: 'QU-2026-0002', lineNumber: 1, productNumber: 'ACME-1001', quantity: 2.0,  unitPriceCent: 149900, discountPercent: 0.0,  hasFixedPrice: false, priceOverridden: false, reason: null},
  {documentNumber: 'OC-2026-0002', lineNumber: 1, productNumber: 'ACME-1001', quantity: 2.0,  unitPriceCent: 134900, discountPercent: 0.0,  hasFixedPrice: false, priceOverridden: true,  reason: 'Volume agreement for the second unit.'},

  {documentNumber: 'QU-2026-0003', lineNumber: 1, productNumber: 'ACME-2003', quantity: 10.0, unitPriceCent: 2450,   discountPercent: 10.0, hasFixedPrice: false, priceOverridden: false, reason: null},
  {documentNumber: 'QU-2026-0003', lineNumber: 2, productNumber: 'ACME-2002', quantity: 25.0, unitPriceCent: 20,     discountPercent: 10.0, hasFixedPrice: false, priceOverridden: false, reason: null},

  {documentNumber: 'PO-2026-0001', lineNumber: 1, productNumber: 'ACME-2003', quantity: 50.0, unitPriceCent: 1100,   discountPercent: 0.0,  hasFixedPrice: false, priceOverridden: false, reason: null},
  {documentNumber: 'PO-2026-0001', lineNumber: 2, productNumber: 'ACME-2002', quantity: 200.0, unitPriceCent: 6,     discountPercent: 0.0,  hasFixedPrice: false, priceOverridden: false, reason: null},
  {documentNumber: 'GR-2026-0001', lineNumber: 1, productNumber: 'ACME-2003', quantity: 50.0, unitPriceCent: 1100,   discountPercent: 0.0,  hasFixedPrice: false, priceOverridden: false, reason: null},
  {documentNumber: 'GR-2026-0001', lineNumber: 2, productNumber: 'ACME-2002', quantity: 180.0, unitPriceCent: 6,     discountPercent: 0.0,  hasFixedPrice: false, priceOverridden: false, reason: null}
] AS row
MATCH (d:Document {number: row.documentNumber})
MATCH (p:Product  {number: row.productNumber})
MERGE (line:DocumentLine {id: row.documentNumber + '_' + toString(row.lineNumber)})
SET line.lineNumber      = row.lineNumber,
    line.sortOrder       = row.lineNumber * 10,
    line.quantity        = row.quantity,
    line.unitPriceCent   = row.unitPriceCent,
    line.discountPercent = row.discountPercent,
    line.hasFixedPrice   = row.hasFixedPrice,
    line.priceOverridden = row.priceOverridden,
    line.reason          = row.reason
MERGE (d)-[:HAS_LINE]->(line)
MERGE (line)-[:OF_PRODUCT]->(p);

// Order lines are not document lines. A DocumentLine records what is printed on
// a sheet of paper ("1x Starter Kit A"); an OrderLine records what that consists
// of — the bill of materials as it stood when the order was placed. Production
// needs the second, the print layout needs the first.
UNWIND [
  {projectNumber: '2026-0001', lineNumber: 1, productNumber: 'ACME-1000', quantity: 1.0, status: 1, shortText: 'Starter Kit A'},
  {projectNumber: '2026-0002', lineNumber: 1, productNumber: 'ACME-1001', quantity: 2.0, status: 0, shortText: 'Starter Kit B'}
] AS row
MATCH (o:Order   {projectNumber: row.projectNumber})
MATCH (p:Product {number: row.productNumber})
MERGE (line:OrderLine {id: row.projectNumber + '_' + toString(row.lineNumber)})
SET line.lineNumber = row.lineNumber,
    line.quantity   = row.quantity,
    line.status     = row.status,
    line.shortText  = row.shortText
MERGE (o)-[:HAS_LINE]->(line)
MERGE (line)-[:OF_PRODUCT]->(p);

// The parts each order line needs, taken from the bill of materials.
MATCH (line:OrderLine)-[:OF_PRODUCT]->(assembly:Product)-[c:CONTAINS]->(part:Product)
MERGE (line)-[r:REQUIRES]->(part)
SET r.quantity = c.quantity * line.quantity;


// ----------------------------------------------------------------------------
// 11) STOCK MOVEMENTS
//
// The opening balance is booked as a movement rather than written into
// StockLevel.quantity directly. Kept as an event, "quantity = f(movements)"
// stays true and section 13 can rebuild the cache from scratch at any time.
//
// Four types: Receipt and Correction add, Issue subtracts, Reservation only
// blocks. quantity is always positive — the direction sits in the type.
// ----------------------------------------------------------------------------
UNWIND [
  {productNumber: 'ACME-1000', locationId: '1', quantity: 3.0},
  {productNumber: 'ACME-1001', locationId: '1', quantity: 5.0},
  {productNumber: 'ACME-2001', locationId: '1', quantity: 850.0},
  {productNumber: 'ACME-2002', locationId: '1', quantity: 320.0},
  {productNumber: 'ACME-2003', locationId: '1', quantity: 12.0},
  // Three products deliberately sit below their minimum stock, so the reorder
  // analysis in procurement has something to report: ACME-2004, -2006 and -2007.
  {productNumber: 'ACME-2004', locationId: '1', quantity: 1.0},
  {productNumber: 'ACME-2005', locationId: '1', quantity: 95.0},
  {productNumber: 'ACME-2006', locationId: '1', quantity: 2.0},
  {productNumber: 'ACME-2007', locationId: '1', quantity: 6.0},
  {productNumber: 'ACME-2008', locationId: '1', quantity: 140.0},
  {productNumber: 'ACME-2009', locationId: '1', quantity: 60.0},
  {productNumber: 'ACME-2002', locationId: '2', quantity: 40.0},
  {productNumber: 'ACME-2003', locationId: '2', quantity: 4.0},
  {productNumber: 'ACME-2005', locationId: '2', quantity: 6.0},
  {productNumber: 'ACME-2002', locationId: '3', quantity: 35.0},
  {productNumber: 'ACME-2003', locationId: '3', quantity: 2.0}
] AS row
MATCH (p:Product  {number: row.productNumber})
MATCH (l:Location {id: row.locationId})
MERGE (s:StockLevel {id: row.productNumber + '_' + row.locationId})
ON CREATE SET s.quantity = 0.0, s.reserved = 0.0
MERGE (p)-[:HAS_STOCK]->(s)
MERGE (s)-[:AT_LOCATION]->(l)
MERGE (m:StockMovement {id: 'OPENING-' + row.productNumber + '_' + row.locationId})
ON CREATE SET m.type      = 'Correction',
              m.quantity  = row.quantity,
              m.createdAt = datetime(),
              m.note      = 'Opening balance from the seed script'
MERGE (m)-[:POSTED_TO]->(s);

// Transactional movements. The goods receipt settles PO-2026-0001, the issue
// settles DN-2026-0001, and one open reservation blocks stock without moving it.
UNWIND [
  {id: 'MOV-0001', productNumber: 'ACME-2003', locationId: '1', type: 'Receipt',     quantity: 50.0,  date: '2026-02-12', documentNumber: 'GR-2026-0001', purchasePriceCent: 1100, customerId: null,     note: 'Goods receipt from Northern Supply GmbH'},
  {id: 'MOV-0002', productNumber: 'ACME-2002', locationId: '1', type: 'Receipt',     quantity: 180.0, date: '2026-02-12', documentNumber: 'GR-2026-0001', purchasePriceCent: 6,    customerId: null,     note: 'Goods receipt from Northern Supply GmbH'},
  {id: 'MOV-0003', productNumber: 'ACME-1000', locationId: '1', type: 'Issue',       quantity: 1.0,   date: '2026-03-18', documentNumber: 'DN-2026-0001', purchasePriceCent: null, customerId: 'C-1001', note: 'Shipped to the customer'},
  {id: 'MOV-0004', productNumber: 'ACME-1001', locationId: '1', type: 'Reservation', quantity: 2.0,   date: '2026-04-10', documentNumber: 'OC-2026-0002', purchasePriceCent: null, customerId: 'C-1002', note: 'Reserved for the confirmed order'},
  {id: 'MOV-0005', productNumber: 'ACME-2003', locationId: '2', type: 'Issue',       quantity: 2.0,   date: '2026-04-03', documentNumber: null,           purchasePriceCent: null, customerId: 'C-1001', note: 'Consumed during a service call'}
] AS row
MATCH (p:Product  {number: row.productNumber})
MATCH (l:Location {id: row.locationId})
MERGE (s:StockLevel {id: row.productNumber + '_' + row.locationId})
ON CREATE SET s.quantity = 0.0, s.reserved = 0.0
MERGE (p)-[:HAS_STOCK]->(s)
MERGE (s)-[:AT_LOCATION]->(l)
MERGE (m:StockMovement {id: row.id})
SET m.type              = row.type,
    m.quantity          = row.quantity,
    m.createdAt         = datetime({date: date(row.date), time: time('08:00:00Z')}),
    m.documentNumber    = row.documentNumber,
    m.purchasePriceCent = row.purchasePriceCent,
    m.note              = row.note
MERGE (m)-[:POSTED_TO]->(s);

// Document reference and customer reference in their own passes, for the same
// reason as above: MATCH on both sides instead of MERGE.
UNWIND [
  {id: 'MOV-0001', documentNumber: 'GR-2026-0001'},
  {id: 'MOV-0002', documentNumber: 'GR-2026-0001'},
  {id: 'MOV-0003', documentNumber: 'DN-2026-0001'},
  {id: 'MOV-0004', documentNumber: 'OC-2026-0002'}
] AS row
MATCH (m:StockMovement {id: row.id})
MATCH (d:Document      {number: row.documentNumber})
MERGE (m)-[:BASED_ON_DOCUMENT]->(d);

// Makes the movement history traceable per customer.
UNWIND [
  {id: 'MOV-0003', customerId: 'C-1001'},
  {id: 'MOV-0004', customerId: 'C-1002'},
  {id: 'MOV-0005', customerId: 'C-1001'}
] AS row
MATCH (m:StockMovement {id: row.id})
MATCH (c:Customer      {id: row.customerId})
MERGE (m)-[:CONCERNS_CUSTOMER]->(c);


// ----------------------------------------------------------------------------
// 12) ASSET INSTANCES
//
// A serialised unit sold to a customer. Its status is not stored: it follows
// from shippedOn and bomReleased, so the two fields can never contradict a
// third. The first unit is shipped and released, the second is still in
// planning — that pair covers all three states the API derives.
//
// The dates run ordered -> released -> shipped -> installed. A unit that never
// shipped carries no installation date either: it is not standing at a
// customer's site yet.
// ----------------------------------------------------------------------------
UNWIND [
  {serialNumber: 'SN-2026-0001', internalNumber: 'ACME-SN-0001', productNumber: 'ACME-1000',
   customerId: 'C-1001', orderedOn: '2026-01-22', shippedOn: '2026-03-18',
   installedOn: '2026-03-20', bomReleased: true,  releasedOn: '2026-03-10', releasedById: '2',
   documentNumber: 'OC-2026-0001', projectNumber: '2026-0001'},
  {serialNumber: 'SN-2026-0002', internalNumber: 'ACME-SN-0002', productNumber: 'ACME-1001',
   customerId: 'C-1002', orderedOn: '2026-04-10', shippedOn: null,
   installedOn: null, bomReleased: false, releasedOn: null,        releasedById: null,
   documentNumber: 'OC-2026-0002', projectNumber: '2026-0002'}
] AS row
MATCH (c:Customer {id: row.customerId})
MATCH (p:Product  {number: row.productNumber})
MERGE (a:AssetInstance {serialNumber: row.serialNumber})
SET a.internalNumber = row.internalNumber,
    a.projectNumber  = row.projectNumber,
    a.orderedOn      = date(row.orderedOn),
    a.shippedOn      = CASE WHEN row.shippedOn IS NULL THEN null ELSE date(row.shippedOn) END,
    a.installedOn    = CASE WHEN row.installedOn IS NULL THEN null ELSE date(row.installedOn) END,
    a.bomReleased    = row.bomReleased,
    a.releasedOn     = CASE WHEN row.releasedOn IS NULL THEN null ELSE date(row.releasedOn) END
MERGE (a)-[:SOLD_TO]->(c)
MERGE (a)-[:BASED_ON]->(p);

// Who released the bill of materials, and which order confirmation the unit
// came from. Separate passes so both sides can use MATCH.
UNWIND [
  {serialNumber: 'SN-2026-0001', releasedById: '2'}
] AS row
MATCH (a:AssetInstance {serialNumber: row.serialNumber})
MATCH (e:Employee      {id: row.releasedById})
MERGE (a)-[:RELEASED_BY]->(e);

UNWIND [
  {serialNumber: 'SN-2026-0001', documentNumber: 'OC-2026-0001'},
  {serialNumber: 'SN-2026-0002', documentNumber: 'OC-2026-0002'}
] AS row
MATCH (a:AssetInstance {serialNumber: row.serialNumber})
MATCH (d:Document      {number: row.documentNumber})
MERGE (a)-[:BASED_ON_DOCUMENT]->(d);


// ----------------------------------------------------------------------------
// 13) DERIVED STRUCTURES
//
// Everything that follows from the finished graph rather than from the data
// above. These steps must run last: earlier sections create products through
// MERGE, and a classification handed out too early would miss exactly those.
// ----------------------------------------------------------------------------

// --- 13.1 Assembly / Part classification ------------------------------------
// Derived from the structure rather than stored as a flag. A flag can go stale
// when a bill of materials changes; the structure is the truth.
MATCH (p:Product) WHERE (p)-[:CONTAINS]->()
SET p:Assembly REMOVE p:Part;

MATCH (p:Product) WHERE NOT (p)-[:CONTAINS]->()
SET p:Part REMOVE p:Assembly;


// --- 13.2 Stock cache from the movement history -----------------------------
// Runs before 13.3 so the digital twin sees real stock figures instead of the
// 0.0 written by ON CREATE.
MATCH (s:StockLevel)
OPTIONAL MATCH (s)<-[:POSTED_TO]-(m:StockMovement)
WITH s,
     sum(CASE m.type
           WHEN 'Receipt'    THEN m.quantity
           WHEN 'Issue'      THEN -m.quantity
           WHEN 'Correction' THEN m.quantity
           ELSE 0.0 END) AS newQuantity,
     sum(CASE m.type
           WHEN 'Reservation' THEN m.quantity
           WHEN 'Issue'       THEN -m.quantity
           ELSE 0.0 END) AS newReserved
SET s.quantity = toFloat(newQuantity),
    // An issue without a preceding reservation would drive reserved negative.
    s.reserved = CASE WHEN newReserved < 0 THEN 0.0 ELSE toFloat(newReserved) END;


// --- 13.3 Digital twin ------------------------------------------------------
// One ComponentInstance per leaf of the sold assembly's bill of materials. Only
// leaves: "WHERE NOT (leaf)-[:CONTAINS]->()" keeps the longest path of every
// branch, so a nested assembly such as ACME-2004 is walked through rather than
// recorded as a component of its own.
//
// quantity is the product of the edge quantities along the path, summed over
// all paths reaching the same leaf — the same part can sit in more than one
// sub-assembly.
//
// The key matches the runtime path (serialNumber + '_' + productNumber), which
// is what keeps MERGE idempotent and a re-run free of duplicates.
MATCH (a:AssetInstance)-[:BASED_ON]->(root:Product)
MATCH path = (root)-[:CONTAINS*0..]->(leaf:Product)
WHERE NOT (leaf)-[:CONTAINS]->()
WITH a, leaf, sum(reduce(q = 1.0, rel IN relationships(path) | q * rel.quantity)) AS required
MERGE (ci:ComponentInstance {id: a.serialNumber + '_' + leaf.number})
ON CREATE SET ci.status = 'active', ci.installedOn = a.installedOn
MERGE (a)-[r:HAS_COMPONENT]->(ci)
ON CREATE SET r.quantity = required
MERGE (ci)-[:IS_TYPE]->(leaf);

// Any product a sold asset is based on behaves as an assembly in the warehouse:
// a future order confirmation for it has to create an asset again instead of
// treating it as an ordinary stock item.
MATCH (:AssetInstance)-[:BASED_ON]->(p:Product)
SET p.stockEffect = 'billOfMaterials';


// --- 13.4 Compensate negative stock -----------------------------------------
// Negative stock is impossible in the real world. It appears where more was
// issued than ever received — a made-to-order unit that never passed through
// the warehouse, for instance. Rather than overwriting the value silently, a
// traceable correction is booked, so "quantity = f(movements)" stays true and
// the history explains the number.
MATCH (s:StockLevel)
WHERE s.quantity < 0
WITH s, -s.quantity AS compensation
MERGE (m:StockMovement {id: 'BALANCE-' + s.id})
ON CREATE SET m.type      = 'Correction',
              m.quantity  = compensation,
              m.createdAt = datetime(),
              m.note      = 'Compensation of a negative balance during seeding (section 13.4)'
MERGE (m)-[:POSTED_TO]->(s)
SET s.quantity = 0.0;


// ----------------------------------------------------------------------------
// 14) VERIFICATION
//
// Prints one row per label so a failed seed is visible immediately instead of
// surfacing as an empty list in the API hours later.
// ----------------------------------------------------------------------------
MATCH (n)
UNWIND labels(n) AS label
RETURN label, count(*) AS count
ORDER BY label;
