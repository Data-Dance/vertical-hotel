# Copyright (C) 2024-TODAY Serpent Consulting Services Pvt. Ltd. (<http://www.serpentcs.com>).
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from datetime import timedelta

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.fields import Command
from odoo.tests import Form, TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install")
class TestHotel(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.env.company.id)], limit=1
        )
        cls.partner = cls.env["res.partner"].create({"name": "Test Partner"})
        cls.checkin_date = fields.Datetime.now().replace(microsecond=0) + timedelta(
            days=1
        )
        cls.checkout_date = cls.checkin_date + timedelta(days=2)
        cls.room_type = cls.env["hotel.room.type"].create({"name": "Test Room Type"})
        cls.room = cls._create_room("Test Room")
        cls.hotel_folio = cls._create_folio()

    @classmethod
    def _create_room(cls, name, **vals):
        return cls.env["hotel.room"].create(
            {
                "name": name,
                "list_price": 100.0,
                "capacity": 2,
                "isroom": True,
                "room_categ_id": cls.room_type.id,
                **vals,
            }
        )

    @classmethod
    def _create_folio(cls):
        return cls.env["hotel.folio"].create(
            {
                "warehouse_id": cls.warehouse.id,
                "partner_id": cls.partner.id,
                "partner_invoice_id": cls.partner.id,
                "partner_shipping_id": cls.partner.id,
            }
        )

    def _add_room_line(self, folio, room, checkin=None, checkout=None):
        return self.env["hotel.folio.line"].create(
            {
                "folio_id": folio.id,
                "product_id": room.product_id.id,
                "checkin_date": checkin or self.checkin_date,
                "checkout_date": checkout or self.checkout_date,
                "product_uom_qty": 2.0,
            }
        )

    def _available(self, checkin, checkout, name="Test Room"):
        products = (
            self.env["product.product"]
            .with_context(
                from_room=True,
                checkin_date=fields.Datetime.to_string(checkin),
                checkout_date=fields.Datetime.to_string(checkout),
            )
            .name_search(name)
        )
        return [product_id for product_id, __ in products]

    # Folio / lines --------------------------------------------------------

    def test_folio_sequence_and_display_name(self):
        self.assertTrue(self.hotel_folio.name.startswith("Folio/"))
        self.assertEqual(self.hotel_folio.display_name, self.hotel_folio.order_id.name)
        found = self.env["hotel.folio"].name_search(self.hotel_folio.name)
        self.assertIn(self.hotel_folio.id, [folio_id for folio_id, __ in found])

    def test_room_and_service_products_are_services(self):
        self.assertEqual(self.room.type, "service")
        service = self.env["hotel.services"].create(
            {
                "name": "Laundry",
                "service_categ_id": self.env["hotel.service.type"]
                .create({"name": "Cleaning"})
                .id,
            }
        )
        self.assertEqual(service.type, "service")

    def test_folio_line_onchange_product_id(self):
        """Folio line onchange fills the sale.order.line fields."""
        folio_line = self._add_room_line(self.hotel_folio, self.room)
        self.assertFalse(folio_line._onchange_product_id_warning())
        self.assertEqual(folio_line.name, self.room.product_id.name)
        self.assertEqual(folio_line.product_uom_id, self.room.product_id.uom_id)
        self.assertEqual(folio_line.price_unit, self.room.product_id.list_price)
        self.assertEqual(
            folio_line.tax_ids,
            self.room.product_id.taxes_id._filter_taxes_by_company(
                self.hotel_folio.company_id
            ),
        )

    def test_folio_line_onchange_taxes_of_folio_company_only(self):
        """Taxes of other companies are never copied on the line."""
        other_company = self.env["res.company"].create(
            {"name": "Other Hotel Co", "country_id": self.env.ref("base.be").id}
        )
        tax_group = self.env["account.tax.group"].create(
            {"name": "Other group", "company_id": other_company.id}
        )
        other_tax = self.env["account.tax"].create(
            {
                "name": "Other 7%",
                "amount": 7,
                "company_id": other_company.id,
                "tax_group_id": tax_group.id,
            }
        )
        self.room.product_id.taxes_id |= other_tax
        folio_line = self._add_room_line(self.hotel_folio, self.room)
        folio_line._onchange_product_id_warning()
        self.assertNotIn(other_tax, folio_line.tax_ids)

    def test_folio_line_onchange_product_id_warning(self):
        """A product warning message is returned to the user."""
        self.room.product_id.sale_line_warn_msg = "Room under maintenance"
        folio_line = self._add_room_line(self.hotel_folio, self.room)
        result = folio_line._onchange_product_id_warning()
        self.assertEqual(result["warning"]["message"], "Room under maintenance")

    def test_stay_nights_rule(self):
        """Whole days, plus one for a remainder reaching the additional hours;
        never two for the same partial day, never less than one night."""
        company = self.env.company
        checkin = self.checkin_date.replace(hour=14, minute=0, second=0)
        cases = [
            # (hours of stay, additional hours, nights)
            (21, 0, 1),
            (21, 2, 1),
            (21, 21, 1),
            (21, 22, 1),
            (45, 0, 2),
            (45, 2, 2),
            (30, 4, 2),
            (30, 8, 1),
            (48, 0, 2),
            (48, 2, 2),
        ]
        for hours, additional_hours, nights in cases:
            company.additional_hours = additional_hours
            with self.subTest(hours=hours, additional_hours=additional_hours):
                self.assertEqual(
                    company._get_hotel_stay_nights(
                        checkin, checkin + timedelta(hours=hours)
                    ),
                    nights,
                )
        self.assertEqual(company._get_hotel_stay_nights(checkin, False), 0)

    def test_folio_line_dates_onchange(self):
        """14:00 to 11:00 the next day is one night, whatever the setting."""
        checkin = self.checkin_date.replace(hour=14, minute=0, second=0)
        for additional_hours in (0, 2):
            self.env.company.additional_hours = additional_hours
            line = self.env["hotel.folio.line"].new(
                {
                    "folio_id": self.hotel_folio.id,
                    "product_id": self.room.product_id.id,
                    "checkin_date": checkin,
                    "checkout_date": checkin + timedelta(hours=21),
                }
            )
            line._onchange_checkin_checkout_dates()
            self.assertEqual(line.product_uom_qty, 1)

    def test_folio_line_quantity_without_onchange(self):
        """A line created or re-dated outside the form bills its nights."""
        line = self.env["hotel.folio.line"].create(
            {
                "folio_id": self.hotel_folio.id,
                "product_id": self.room.product_id.id,
                "checkin_date": self.checkin_date,
                "checkout_date": self.checkin_date + timedelta(days=5),
            }
        )
        self.assertEqual(line.product_uom_qty, 5)
        self.assertEqual(line.price_subtotal, 500)
        line.write({"checkout_date": self.checkin_date + timedelta(days=3)})
        self.assertEqual(line.product_uom_qty, 3)
        # a quantity given with the dates is kept
        line.write(
            {
                "checkout_date": self.checkin_date + timedelta(days=4),
                "product_uom_qty": 1,
            }
        )
        self.assertEqual(line.product_uom_qty, 1)
        line = self._add_room_line(
            self.hotel_folio,
            self._create_room("Test Room 2"),
            checkout=self.checkin_date + timedelta(days=5),
        )
        self.assertEqual(line.product_uom_qty, 2)

    def test_service_line_onchange_product_id(self):
        """Service line onchange fills the sale.order.line fields."""
        service = self.env["product.product"].create(
            {"name": "Test Service", "list_price": 50.0, "isservice": True}
        )
        service_line = self.env["hotel.service.line"].create(
            {
                "folio_id": self.hotel_folio.id,
                "product_id": service.id,
                "product_uom_qty": 1.0,
                "ser_checkin_date": self.checkin_date,
                "ser_checkout_date": self.checkout_date,
            }
        )
        self.assertFalse(service_line._onchange_product_id_warning())
        self.assertEqual(service_line.name, service.name)
        self.assertEqual(service_line.product_uom_id, service.uom_id)
        self.assertEqual(service_line.price_unit, service.list_price)

    def test_service_line_onchange_product_id_warning(self):
        service = self.env["product.product"].create(
            {
                "name": "Warned Service",
                "list_price": 50.0,
                "isservice": True,
                "sale_line_warn_msg": "Service not available",
            }
        )
        service_line = self.env["hotel.service.line"].create(
            {
                "folio_id": self.hotel_folio.id,
                "product_id": service.id,
                "product_uom_qty": 1.0,
            }
        )
        result = service_line._onchange_product_id_warning()
        self.assertEqual(result["warning"]["message"], "Service not available")

    def test_service_line_dates_onchange(self):
        """Only the check-in date set: checkout defaults to it (1 day)."""
        service_line = self.env["hotel.service.line"].new(
            {"ser_checkin_date": self.checkin_date}
        )
        service_line._on_change_checkin_checkout_dates()
        self.assertEqual(service_line.ser_checkout_date, self.checkin_date)
        self.assertEqual(service_line.product_uom_qty, 1)

    # Prices ---------------------------------------------------------------

    def _pricelist(self, **rule):
        return self.env["product.pricelist"].create(
            {
                "name": "Test Hotel Pricelist",
                "item_ids": [Command.create({"applied_on": "3_global", **rule})],
            }
        )

    def _fill_folio_form(self, folio, room_price=None):
        """Add a room and a service to the folio the way the form does."""
        service = self.env["product.product"].create(
            {"name": "Test Laundry", "list_price": 40.0, "isservice": True}
        )
        with Form(folio) as folio_form:
            with folio_form.room_line_ids.new() as line:
                line.checkin_date = self.checkin_date
                line.checkout_date = self.checkout_date
                line.product_id = self.room.product_id
                if room_price:
                    line.price_unit = room_price
            with folio_form.service_line_ids.new() as line:
                line.product_id = service
        return folio.room_line_ids, folio.service_line_ids

    def test_price_from_fixed_pricelist(self):
        self.hotel_folio.pricelist_id = self._pricelist(
            compute_price="fixed", fixed_price=50.0
        )
        room_line, service_line = self._fill_folio_form(self.hotel_folio)
        self.assertEqual(room_line.price_unit, 50.0)
        self.assertEqual(room_line.price_subtotal, 100.0)
        self.assertEqual(service_line.price_unit, 50.0)
        # and without the form
        line = self._add_room_line(self.hotel_folio, self._create_room("Test Room 2"))
        self.assertEqual(line.price_unit, 50.0)
        line._onchange_product_id_warning()
        self.assertEqual(line.price_unit, 50.0)

    def test_price_from_discount_pricelist(self):
        self.hotel_folio.pricelist_id = self._pricelist(
            compute_price="discount", price_discount=20.0
        )
        room_line, service_line = self._fill_folio_form(self.hotel_folio)
        # shown as a lower price or as a discount, depending on the settings
        self.assertEqual(room_line.price_subtotal, 2 * 80.0)
        self.assertEqual(
            service_line.price_subtotal, service_line.product_uom_qty * 32.0
        )

    def test_price_from_discount_pricelist_shown_as_discount(self):
        self.env.ref("base.group_user").implied_ids |= self.env.ref(
            "sale.group_discount_per_so_line"
        )
        self.hotel_folio.pricelist_id = self._pricelist(
            compute_price="discount", price_discount=20.0
        )
        room_line, _service_line = self._fill_folio_form(self.hotel_folio)
        self.assertEqual(room_line.price_unit, 100.0)
        self.assertEqual(room_line.discount, 20.0)
        self.assertEqual(room_line.price_subtotal, 2 * 80.0)

    def test_price_on_a_new_folio(self):
        """The pricelist applies before the folio is saved, too."""
        pricelist = self._pricelist(compute_price="fixed", fixed_price=50.0)
        with Form(self.env["hotel.folio"]) as folio_form:
            folio_form.partner_id = self.partner
            folio_form.pricelist_id = pricelist
            with folio_form.room_line_ids.new() as line:
                line.checkin_date = self.checkin_date
                line.checkout_date = self.checkout_date
                line.product_id = self.room.product_id
                self.assertEqual(line.price_unit, 50.0)
        self.assertEqual(folio_form.record.room_line_ids.price_unit, 50.0)

    def test_price_without_pricelist_rule(self):
        self.hotel_folio.pricelist_id = self.env["product.pricelist"].create(
            {"name": "Test Empty Pricelist"}
        )
        room_line, service_line = self._fill_folio_form(self.hotel_folio)
        self.assertEqual(room_line.price_unit, 100.0)
        self.assertEqual(service_line.price_unit, 40.0)

    def test_price_typed_by_the_user_is_kept(self):
        self.hotel_folio.pricelist_id = self._pricelist(
            compute_price="fixed", fixed_price=50.0
        )
        room_line, _service_line = self._fill_folio_form(
            self.hotel_folio, room_price=70.0
        )
        self.assertEqual(room_line.price_unit, 70.0)
        # one more night: the typed price stays
        with (
            Form(self.hotel_folio) as folio_form,
            folio_form.room_line_ids.edit(0) as line,
        ):
            line.checkout_date = self.checkout_date + timedelta(days=1)
        self.assertEqual(room_line.product_uom_qty, 3)
        self.assertEqual(room_line.price_unit, 70.0)

    # Workflow -------------------------------------------------------------

    def test_confirm_sale(self):
        self._add_room_line(self.hotel_folio, self.room)
        self.hotel_folio.action_confirm()
        self.assertEqual(self.hotel_folio.state, "sale")
        self.assertEqual(self.hotel_folio.invoice_status, "to invoice")
        # rooms are services: no delivery order for a night in a room
        self.assertFalse(self.hotel_folio.order_id.picking_ids)

    def test_folio_cancel_frees_rooms(self):
        self._add_room_line(self.hotel_folio, self.room)
        self.assertEqual(self.room.status, "occupied")
        self.hotel_folio.action_cancel()
        self.assertEqual(self.hotel_folio.state, "cancel")
        self.assertEqual(self.room.status, "available")

    def test_folio_set_to_draft(self):
        self.hotel_folio.action_cancel()
        self.hotel_folio.action_cancel_draft()
        self.assertEqual(self.hotel_folio.state, "draft")

    def test_create_invoice_links_folio(self):
        self._add_room_line(self.hotel_folio, self.room)
        self.hotel_folio.action_confirm()
        action = self.hotel_folio.action_create_invoice()
        wizard = (
            self.env["sale.advance.payment.inv"]
            .with_context(**action["context"])
            .create({})
        )
        self.assertEqual(wizard.sale_order_ids, self.hotel_folio.order_id)
        wizard.create_invoices()
        self.assertTrue(self.hotel_folio.hotel_invoice_id)
        self.assertEqual(self.hotel_folio.invoice_status, "invoiced")

    # Room availability ----------------------------------------------------

    def test_search_fetch_from_room(self):
        """from_room context keeps only the rooms free on the given dates."""
        product = self.room.product_id
        stay = (self.checkin_date, self.checkout_date)
        self.assertIn(product.id, self._available(*stay))
        self._add_room_line(self.hotel_folio, self.room)
        # overlapping stay
        self.assertNotIn(
            product.id,
            self._available(
                self.checkin_date + timedelta(hours=1),
                self.checkout_date + timedelta(days=1),
            ),
        )
        # stay starting when the booked one ends
        self.assertIn(
            product.id,
            self._available(self.checkout_date, self.checkout_date + timedelta(days=1)),
        )
        # a cancelled folio does not block the room
        self.hotel_folio.action_cancel()
        self.assertIn(product.id, self._available(*stay))

    def test_search_fetch_from_room_only_rooms(self):
        """The availability filter never returns non-room products."""
        self.env["product.product"].create({"name": "Test Room Service"})
        found = self._available(self.checkin_date, self.checkout_date, "Test Room")
        self.assertEqual(found, [self.room.product_id.id])

    def test_double_booking_constraint(self):
        self._add_room_line(self.hotel_folio, self.room)
        with self.assertRaises(ValidationError):
            self._add_room_line(
                self.hotel_folio,
                self.room,
                self.checkin_date + timedelta(hours=2),
                self.checkout_date + timedelta(hours=2),
            )

    # Categories -----------------------------------------------------------

    def test_room_type_hierarchical_name_search(self):
        child = self.env["hotel.room.type"].create(
            {"name": "Deluxe", "categ_id": self.room_type.id}
        )
        self.assertEqual(child.display_name, "Test Room Type / Deluxe")
        found = self.env["hotel.room.type"].name_search("Test Room Type / Del")
        self.assertEqual([type_id for type_id, __ in found], [child.id])

    def test_amenity_and_service_type_hierarchical_name_search(self):
        for model, parent_field in (
            ("hotel.room.amenities.type", "amenity_id"),
            ("hotel.service.type", "service_id"),
        ):
            parent = self.env[model].create({"name": "Parent X"})
            child = self.env[model].create({"name": "Child Y", parent_field: parent.id})
            found = self.env[model].name_search("Parent X / Child")
            self.assertEqual([rec_id for rec_id, __ in found], [child.id], model)

    # Report ---------------------------------------------------------------

    def test_report_folio_data(self):
        """Confirmed folios whose stay overlaps the period, whatever the
        dates on the folio header (creation time by default)."""
        report = self.env["report.vertical_hotel.report_hotel_folio"]
        draft_folio = self._create_folio()
        self._add_room_line(draft_folio, self._create_room("Draft Room"))
        cancelled_folio = self._create_folio()
        self._add_room_line(cancelled_folio, self._create_room("Cancelled Room"))
        cancelled_folio.action_cancel()
        later_folio = self._create_folio()
        self._add_room_line(
            later_folio,
            self._create_room("Later Room"),
            checkin=self.checkin_date + timedelta(days=30),
            checkout=self.checkout_date + timedelta(days=30),
        )
        later_folio.action_confirm()
        self._add_room_line(self.hotel_folio, self.room)
        self.hotel_folio.action_confirm()

        def rows(date_start, date_end):
            data = report._get_folio_data(date_start, date_end)
            self.assertEqual(
                data[-1]["total_amount"],
                sum(row["amount"] for row in data[:-1]),
            )
            return {row["name"]: row for row in data[:-1]}

        # the period starts after the folios were created, and ends in the
        # middle of the stay
        found = rows(
            self.checkin_date - timedelta(hours=1),
            self.checkin_date + timedelta(days=1),
        )
        self.assertEqual(list(found), [self.hotel_folio.name])
        row = found[self.hotel_folio.name]
        self.assertEqual(row["checkin"], self.checkin_date)
        self.assertEqual(row["checkout"], self.checkout_date)
        self.assertEqual(row["amount"], self.hotel_folio.amount_total)
        # the folio header dates do not select anything
        self.assertFalse(
            rows(
                self.hotel_folio.checkin_date - timedelta(hours=1),
                self.hotel_folio.checkout_date - timedelta(hours=1),
            )
        )
        self.assertEqual(
            list(
                rows(
                    self.checkin_date - timedelta(days=5),
                    self.checkout_date + timedelta(days=40),
                )
            ),
            [self.hotel_folio.name, later_folio.name],
        )

    def test_report_wizard(self):
        # without a document layout, report_action() opens the layout setup
        self.env.company.external_report_layout_id = self.env.ref(
            "web.external_layout_standard"
        )
        wizard = Form(self.env["folio.report.wizard"])
        wizard.date_start = self.checkin_date - timedelta(days=5)
        wizard.date_end = self.checkout_date + timedelta(days=5)
        action = wizard.save().print_report()
        self.assertEqual(action["report_name"], "vertical_hotel.report_hotel_folio")


@tagged("post_install", "-at_install")
class TestHotelSecurity(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.hotel_user = new_test_user(
            cls.env, login="hotel_user", groups="vertical_hotel.group_hotel_user"
        )
        cls.hotel_manager = new_test_user(
            cls.env, login="hotel_manager", groups="vertical_hotel.group_hotel_manager"
        )
        cls.internal_user = new_test_user(
            cls.env, login="plain_user", groups="base.group_user"
        )

    def test_admin_is_hotel_manager(self):
        self.assertIn(
            self.env.ref("vertical_hotel.group_hotel_manager"),
            self.env.ref("base.user_admin").all_group_ids,
        )

    def test_hotel_user_can_work_with_folios(self):
        folio = (
            self.env["hotel.folio"]
            .with_user(self.hotel_user)
            .create(
                {"partner_id": self.env["res.partner"].create({"name": "Guest"}).id}
            )
        )
        self.assertTrue(folio.name)
        with self.assertRaises(AccessError):
            folio.unlink()

    def test_hotel_user_can_add_room_line(self):
        """Adding a room flags it as taken, which writes on hotel.room and so
        on its product and product template."""
        room = self.env["hotel.room"].create(
            {
                "name": "Security Room",
                "list_price": 100.0,
                "capacity": 2,
                "room_categ_id": self.env["hotel.room.type"]
                .create({"name": "Security Room Type"})
                .id,
            }
        )
        checkin = fields.Datetime.now().replace(microsecond=0) + timedelta(days=1)
        folio = (
            self.env["hotel.folio"]
            .with_user(self.hotel_user)
            .create(
                {"partner_id": self.env["res.partner"].create({"name": "Guest"}).id}
            )
        )
        self.env["hotel.folio.line"].with_user(self.hotel_user).create(
            {
                "folio_id": folio.id,
                "product_id": room.product_id.id,
                "checkin_date": checkin,
                "checkout_date": checkin + timedelta(days=2),
                "product_uom_qty": 2.0,
            }
        )
        self.assertEqual(folio.room_line_ids.product_id, room.product_id)
        self.assertFalse(room.isroom)

    def test_hotel_user_can_remove_draft_lines_only(self):
        """A wrong room or service is removed from a draft folio; the lines
        of a confirmed folio stay."""
        room = self.env["hotel.room"].create(
            {
                "name": "Security Room",
                "list_price": 100.0,
                "capacity": 2,
                "room_categ_id": self.env["hotel.room.type"]
                .create({"name": "Security Room Type"})
                .id,
            }
        )
        service = self.env["product.product"].create(
            {"name": "Security Service", "list_price": 50.0, "isservice": True}
        )
        checkin = fields.Datetime.now().replace(microsecond=0) + timedelta(days=1)
        user_env = self.env(user=self.hotel_user)
        folio = user_env["hotel.folio"].create(
            {"partner_id": self.env["res.partner"].create({"name": "Guest"}).id}
        )

        def add_lines():
            room_line = user_env["hotel.folio.line"].create(
                {
                    "folio_id": folio.id,
                    "product_id": room.product_id.id,
                    "checkin_date": checkin,
                    "checkout_date": checkin + timedelta(days=2),
                    "product_uom_qty": 2.0,
                }
            )
            service_line = user_env["hotel.service.line"].create(
                {
                    "folio_id": folio.id,
                    "product_id": service.id,
                    "product_uom_qty": 1.0,
                }
            )
            return room_line, service_line

        room_line, service_line = add_lines()
        room_line.unlink()
        service_line.unlink()
        self.assertFalse(folio.order_id.order_line)
        self.assertTrue(room.isroom)

        room_line, service_line = add_lines()
        folio.action_confirm()
        with self.assertRaises(UserError):
            room_line.unlink()
        with self.assertRaises(UserError):
            service_line.unlink()
        self.assertEqual(len(folio.order_id.order_line), 2)

    def test_checkin_day_is_the_hotel_day(self):
        """A check-in earlier today is fine, "today" being the hotel's day;
        before that, only a manager may enter it."""
        self.env.company.partner_id.tz = "Europe/Rome"  # UTC+2 in June
        room = self.env["hotel.room"].create(
            {
                "name": "Security Room",
                "list_price": 100.0,
                "capacity": 2,
                "room_categ_id": self.env["hotel.room.type"]
                .create({"name": "Security Room Type"})
                .id,
            }
        )
        partner = self.env["res.partner"].create({"name": "Guest"})
        to_datetime = fields.Datetime.to_datetime

        def add_line(user, checkin):
            folio = (
                self.env["hotel.folio"]
                .with_user(user)
                .create({"partner_id": partner.id})
            )
            return (
                self.env["hotel.folio.line"]
                .with_user(user)
                .create(
                    {
                        "folio_id": folio.id,
                        "product_id": room.product_id.id,
                        "checkin_date": to_datetime(checkin),
                        "checkout_date": to_datetime("2027-06-12 08:00:00"),
                    }
                )
            )

        with freeze_time("2027-06-11 07:00:00"):  # 09:00 at the hotel
            # 00:30 today at the hotel, yesterday in UTC
            line = add_line(self.hotel_user, "2027-06-10 22:30:00")
            # 23:30 yesterday at the hotel
            with self.assertRaises(ValidationError):
                line.checkin_date = to_datetime("2027-06-10 21:30:00")
            line.unlink()
            with self.assertRaises(ValidationError):
                add_line(self.hotel_user, "2027-06-10 21:30:00")
            line = add_line(self.hotel_manager, "2027-06-08 12:00:00")
            # the front desk goes on working with the back-dated line
            line.with_user(self.hotel_user).checkout_date = to_datetime(
                "2027-06-13 08:00:00"
            )

    def test_internal_user_has_no_hotel_access(self):
        with self.assertRaises(AccessError):
            self.env["hotel.folio"].with_user(self.internal_user).search([])
