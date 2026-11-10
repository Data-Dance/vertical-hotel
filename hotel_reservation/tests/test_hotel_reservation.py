# Copyright (C) 2024-TODAY Serpent Consulting Services Pvt. Ltd. (<http://www.serpentcs.com>).
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Command
from odoo.tests import common, tagged


@tagged("post_install", "-at_install")
class TestReservation(common.TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.env.company.external_report_layout_id = cls.env.ref(
            "web.external_layout_standard"
        )
        cls.partner = cls.env["res.partner"].create(
            {"name": "Test Guest", "email": "test.guest@example.com"}
        )
        cls.pricelist = cls.env["product.pricelist"].create({"name": "Test Pricelist"})
        cls.room_type = cls.env["hotel.room.type"].create({"name": "Test Single"})
        cls.room = cls._create_room("Test Single-101", 100.0)
        cls.room2 = cls._create_room("Test Single-102", 120.0)
        cls.hotel_user = cls.env["res.users"].create(
            {
                "name": "Test Hotel User",
                "login": "test_hotel_user_reservation",
                "group_ids": [
                    Command.set(
                        [
                            cls.env.ref("base.group_user").id,
                            cls.env.ref("vertical_hotel.group_hotel_user").id,
                        ]
                    )
                ],
            }
        )
        # whole days from now, so the stays never start in the past
        cls.base = datetime.combine(
            fields.Date.today() + timedelta(days=10), time(12, 0)
        )

    @classmethod
    def _create_room(cls, name, price):
        return cls.env["hotel.room"].create(
            {
                "name": name,
                "list_price": price,
                "capacity": 2,
                "room_categ_id": cls.room_type.id,
            }
        )

    def _reservation(
        self, rooms=None, start=0, nights=2, lines=None, user=None, **vals
    ):
        rooms = self.room if rooms is None else rooms
        lines = lines or [rooms]
        values = {
            "partner_id": self.partner.id,
            "partner_invoice_id": self.partner.id,
            "partner_order_id": self.partner.id,
            "partner_shipping_id": self.partner.id,
            "pricelist_id": self.pricelist.id,
            "checkin": self.base + timedelta(days=start),
            "checkout": self.base + timedelta(days=start + nights),
            "adults": 1,
            "reservation_line": [
                Command.create(
                    {"categ_id": self.room_type.id, "reserve": [Command.set(r.ids)]}
                )
                for r in lines
            ],
        }
        values.update(vals)
        return (
            self.env["hotel.reservation"]
            .with_user(user or self.env.user)
            .create(values)
        )

    # Workflow

    def test_confirm(self):
        reservation = self._reservation(self.room | self.room2)
        reservation.confirmed_reservation()
        self.assertEqual(reservation.state, "confirm")
        bookings = self.env["hotel.room.reservation.line"].search(
            [("reservation_id", "=", reservation.id)]
        )
        self.assertEqual(bookings.room_id, self.room | self.room2)
        # the stay starts in 10 days: rooms stay available until then
        self.assertEqual(self.room.status, "available")
        with self.assertRaises(UserError):
            reservation.confirmed_reservation()

    def test_confirm_overlap(self):
        self._reservation().confirmed_reservation()
        with self.assertRaises(ValidationError):
            self._reservation(start=1).confirmed_reservation()
        # leaving the day the next guest arrives does not block the room
        back_to_back = self._reservation(start=2)
        back_to_back.confirmed_reservation()
        self.assertEqual(back_to_back.state, "confirm")

    def test_confirm_overlap_with_folio(self):
        self.env["hotel.folio"].create(
            {
                "partner_id": self.partner.id,
                "pricelist_id": self.pricelist.id,
                "checkin_date": self.base,
                "checkout_date": self.base + timedelta(days=2),
                "room_line_ids": [
                    Command.create(
                        {
                            "product_id": self.room.product_id.id,
                            "checkin_date": self.base,
                            "checkout_date": self.base + timedelta(days=2),
                            "product_uom_qty": 2,
                        }
                    )
                ],
            }
        )
        with self.assertRaises(ValidationError):
            self._reservation(start=1).confirmed_reservation()

    def test_room_occupied_during_stay(self):
        reservation = self._reservation(
            date_order=fields.Datetime.now() - timedelta(days=3),
            checkin=fields.Datetime.now() - timedelta(hours=1),
            checkout=fields.Datetime.now() + timedelta(days=1),
        )
        reservation.confirmed_reservation()
        self.assertEqual(self.room.status, "occupied")
        reservation.cancel_reservation()
        self.assertEqual(self.room.status, "available")

    def test_cancel(self):
        draft = self._reservation()
        draft.cancel_reservation()
        self.assertEqual(draft.state, "cancel")
        confirmed = self._reservation(start=5)
        confirmed.confirmed_reservation()
        confirmed.with_user(self.hotel_user).cancel_reservation()
        self.assertEqual(confirmed.state, "cancel")
        self.assertFalse(
            self.env["hotel.room.reservation.line"].search(
                [("reservation_id", "=", confirmed.id)]
            )
        )
        confirmed.set_to_draft_reservation()
        self.assertEqual(confirmed.state, "draft")
        with self.assertRaises(UserError):
            confirmed.set_to_draft_reservation()

    def test_cancel_done(self):
        reservation = self._reservation()
        reservation.confirmed_reservation()
        reservation.create_folio()
        with self.assertRaises(UserError):
            reservation.cancel_reservation()

    def test_cancel_button(self):
        arch = self.env.ref("hotel_reservation.view_hotel_reservation_form").arch
        self.assertIn("invisible=\"state not in ['draft', 'confirm']\"", arch)

    def test_unlink(self):
        draft = self._reservation()
        draft.unlink()
        confirmed = self._reservation()
        confirmed.confirmed_reservation()
        with self.assertRaises(ValidationError):
            confirmed.unlink()

    def test_unlink_line_frees_room(self):
        reservation = self._reservation(lines=[self.room, self.room2])
        reservation.confirmed_reservation()
        reservation.reservation_line[0].unlink()
        bookings = self.env["hotel.room.reservation.line"].search(
            [("reservation_id", "=", reservation.id)]
        )
        self.assertEqual(bookings.room_id, self.room2)

    # Folio

    def test_create_folio(self):
        reservation = self._reservation(lines=[self.room, self.room2])
        reservation.confirmed_reservation()
        reservation.create_folio()
        folios = self.env["hotel.folio"].search(
            [("reservation_id", "=", reservation.id)]
        )
        self.assertEqual(len(folios), 1, "one folio per reservation")
        self.assertEqual(reservation.folio_id, folios)
        self.assertEqual(reservation.no_of_folio, 1)
        self.assertEqual(reservation.state, "done")
        self.assertEqual(
            folios.room_line_ids.product_id,
            (self.room | self.room2).product_id,
        )
        self.assertEqual(len(folios.room_line_ids), 2)
        self.assertEqual(folios.room_line_ids.mapped("product_uom_qty"), [2.0, 2.0])
        # the folio line's own rule gives the same quantity
        line = folios.room_line_ids[0]
        line._onchange_checkin_checkout_dates()
        self.assertEqual(line.product_uom_qty, 2.0)
        action = reservation.open_folio_view()
        self.assertEqual(action["res_id"], folios.id)

    def test_create_folio_uses_reservation_pricelist(self):
        self.pricelist.item_ids = [
            Command.create(
                {"applied_on": "3_global", "compute_price": "fixed", "fixed_price": 50}
            )
        ]
        reservation = self._reservation()
        reservation.confirmed_reservation()
        reservation.create_folio()
        folio = reservation.folio_id
        self.assertEqual(folio.pricelist_id, self.pricelist)
        self.assertEqual(folio.room_line_ids.price_unit, 50.0)
        self.assertEqual(folio.amount_untaxed, 100.0)

    def test_create_folio_without_pricelist_rule(self):
        reservation = self._reservation(self.room2)
        reservation.confirmed_reservation()
        reservation.create_folio()
        self.assertEqual(reservation.folio_id.room_line_ids.price_unit, 120.0)

    def test_create_folio_multi(self):
        first = self._reservation(self.room)
        second = self._reservation(self.room2)
        (first | second).confirmed_reservation()
        (first | second).create_folio()
        self.assertEqual(len(first.folio_id), 1)
        self.assertEqual(len(second.folio_id), 1)
        self.assertNotEqual(first.folio_id, second.folio_id)
        with self.assertRaises(UserError):
            self._reservation(start=5).create_folio()

    def test_folio_dates_sync(self):
        reservation = self._reservation()
        reservation.confirmed_reservation()
        reservation.create_folio()
        new_checkout = self.base + timedelta(days=4)
        reservation.folio_id.write({"checkout_date": new_checkout})
        booking = self.env["hotel.room.reservation.line"].search(
            [("reservation_id", "=", reservation.id)]
        )
        self.assertEqual(booking.check_out, new_checkout)
        # several reserved folio lines at once
        reservation.folio_id.room_line_ids.write({"name": "Renamed"})

    def test_folio_room_choice(self):
        reservation = self._reservation()
        reservation.confirmed_reservation()
        products = (
            self.env["product.product"]
            .with_context(
                from_room=True,
                checkin_date=fields.Datetime.to_string(self.base),
                checkout_date=fields.Datetime.to_string(self.base + timedelta(days=1)),
            )
            .search([("id", "in", (self.room | self.room2).product_id.ids)])
        )
        self.assertEqual(products, self.room2.product_id)

    # Constraints, onchanges, misc

    def test_check_reservation_rooms(self):
        # the main guest + 2 accompanying adults do not fit in a 2-bed room
        guests = [
            Command.create({"name": name, "guest_type": "adult"})
            for name in ("Guest A", "Guest B")
        ]
        with self.assertRaises(ValidationError):
            self._reservation(guest_ids=guests)
        # the main guest always counts as an adult
        self.assertEqual(self._reservation().adults, 1)

    def test_check_in_out_dates(self):
        with self.assertRaises(ValidationError):
            self._reservation(nights=-1)
        first, second = self._reservation(), self._reservation(start=3)
        (first | second).write({"checkout": self.base + timedelta(days=8)})

    def test_checkin_day_is_the_hotel_day(self):
        """A check-in earlier today is fine, "today" being the hotel's day;
        before that, only a manager may enter it."""
        self.company.partner_id.tz = "Europe/Rome"  # UTC+2 in June
        to_datetime = fields.Datetime.to_datetime
        checkout = to_datetime("2027-06-12 08:00:00")
        with freeze_time("2027-06-11 07:00:00"):  # 09:00 at the hotel
            # 00:30 today at the hotel, yesterday in UTC
            walk_in = self._reservation(
                user=self.hotel_user,
                checkin=to_datetime("2027-06-10 22:30:00"),
                checkout=checkout,
            )
            # 23:30 yesterday at the hotel
            with self.assertRaises(ValidationError):
                walk_in.checkin = to_datetime("2027-06-10 21:30:00")
            with self.assertRaises(ValidationError):
                self._reservation(
                    self.room2,
                    user=self.hotel_user,
                    checkin=to_datetime("2027-06-08 12:00:00"),
                    checkout=checkout,
                )
            # the admin running the tests is a hotel manager
            back_dated = self._reservation(
                self.room2,
                checkin=to_datetime("2027-06-08 12:00:00"),
                checkout=checkout,
            )
            # the front desk goes on working with the back-dated reservation
            back_dated = back_dated.with_user(self.hotel_user)
            back_dated.checkout = to_datetime("2027-06-13 08:00:00")
            back_dated.confirmed_reservation()
            self.assertEqual(back_dated.state, "confirm")

    def test_copy(self):
        reservation = self._reservation()
        copy = reservation.copy({"date_order": reservation.date_order})
        self.assertEqual(copy.adults, 1)
        self.assertNotEqual(copy.reservation_no, reservation.reservation_no)

    def test_check_overlap(self):
        days = self._reservation().check_overlap(
            self.base.date(), self.base.date() + timedelta(days=1)
        )
        self.assertEqual(len(days), 2)

    def test_available_rooms(self):
        self._reservation().confirmed_reservation()
        other = self._reservation(self.room2, start=1)
        self.assertEqual(other.reservation_line.available_room_ids, self.room2)
        later = self._reservation(self.room2, start=5)
        self.assertEqual(
            later.reservation_line.available_room_ids, self.room | self.room2
        )

    def test_on_change_categ(self):
        line = self.env["hotel.reservation.line"].new({"categ_id": self.room_type.id})
        self.assertIn("warning", line.on_change_categ())

    def test_onchange_partner_id(self):
        reservation = self.env["hotel.reservation"].new({"partner_id": self.partner.id})
        reservation._onchange_partner_id()
        self.assertEqual(reservation.partner_invoice_id, self.partner)

    def test_onchange_check_dates(self):
        reservation = self._reservation()
        duration = reservation._onchange_check_dates(
            checkin_date=reservation.checkin, checkout_date=reservation.checkout
        )["duration"]
        self.assertEqual(duration, 2)
        # a started day counts as a day
        duration = reservation._onchange_check_dates(
            checkin_date=reservation.checkin,
            checkout_date=reservation.checkout + timedelta(hours=3),
        )["duration"]
        self.assertEqual(duration, 3)

    def test_stay_nights_not_counted_twice(self):
        """14:00 to 11:00 the next day is one night with additional hours
        too, on the reservation and on the folio made from it."""
        self.env.company.additional_hours = 2
        checkin = self.base + timedelta(hours=2)
        reservation = self._reservation(
            checkin=checkin, checkout=checkin + timedelta(hours=21)
        )
        duration = reservation._onchange_check_dates(
            checkin_date=reservation.checkin, checkout_date=reservation.checkout
        )["duration"]
        self.assertEqual(duration, 1)
        reservation.confirmed_reservation()
        reservation.create_folio()
        self.assertEqual(reservation.folio_id.duration, 1)
        self.assertEqual(reservation.folio_id.room_line_ids.product_uom_qty, 1)

    def test_send_reservation_mail(self):
        action = self._reservation().action_send_reservation_mail()
        self.assertEqual(action["res_model"], "mail.compose.message")
        self.assertTrue(action["context"]["default_template_id"])

    def test_mail_templates_dates(self):
        """Dates are printed in the hotel's timezone, in the guest's format."""
        self.company.partner_id.tz = "Pacific/Auckland"
        reservation = self._reservation()
        tz = ZoneInfo("Pacific/Auckland")
        local_checkin = reservation.checkin.replace(tzinfo=UTC).astimezone(tz)
        local_checkout = reservation.checkout.replace(tzinfo=UTC).astimezone(tz)
        # 12:00 UTC is the next day over there
        self.assertNotEqual(local_checkin.date(), reservation.checkin.date())
        for xmlid in (
            "hotel_reservation.email_template_hotel_reservation",
            "hotel_reservation.mail_template_reservation_reminder_24hrs",
        ):
            body = self.env.ref(xmlid)._render_field(
                "body_html", reservation.ids, compute_lang=True
            )[reservation.id]
            self.assertIn(f"{local_checkin:%m/%d/%Y %H:%M}", body, xmlid)
            self.assertIn(f"{local_checkout:%m/%d/%Y %H:%M}", body, xmlid)
            self.assertNotIn(str(reservation.checkin), body, xmlid)
            for wrong in ("%Y", "$", "nbsp", "Childrens"):
                self.assertNotIn(wrong, body, xmlid)

    def _reminders(self, reservations):
        return self.env["mail.mail"].search(
            [("model", "=", "hotel.reservation"), ("res_id", "in", reservations.ids)]
        )

    def test_reminder_24hrs(self):
        tomorrow = datetime.combine(fields.Date.today() + timedelta(days=1), time(14))
        due = self._reservation(checkin=tomorrow, checkout=tomorrow + timedelta(days=1))
        due.confirmed_reservation()
        draft = self._reservation(
            self.room2, checkin=tomorrow, checkout=tomorrow + timedelta(days=1)
        )
        later = self._reservation(start=5)
        later.confirmed_reservation()
        reservations = due | draft | later
        self.env["hotel.reservation"].reservation_reminder_24hrs()
        self.assertEqual(self._reminders(reservations).mapped("res_id"), due.ids)
        # running the job again the same day does not send it again
        self.env["hotel.reservation"].reservation_reminder_24hrs()
        self.assertEqual(len(self._reminders(reservations)), 1)
        # the stay is moved: the guest is reminded of the new date
        due.write({"checkout": tomorrow + timedelta(days=2)})
        self.assertTrue(due.reminder_sent)
        due.write({"checkin": tomorrow + timedelta(hours=1)})
        self.assertFalse(due.reminder_sent)
        self.env["hotel.reservation"].reservation_reminder_24hrs()
        self.assertEqual(len(self._reminders(reservations)), 2)

    def test_reminder_24hrs_hotel_timezone(self):
        """Tomorrow is the hotel's tomorrow, not the one of the cron user."""
        self.company.partner_id.tz = "Pacific/Auckland"  # UTC+12 in June
        with freeze_time("2027-06-10 10:00:00"):  # 22:00 at the hotel
            # 08:00 tomorrow at the hotel, still today in UTC
            checkin = fields.Datetime.to_datetime("2027-06-10 20:00:00")
            due = self._reservation(
                checkin=checkin, checkout=checkin + timedelta(days=1)
            )
            # 02:00 the day after tomorrow at the hotel, tomorrow in UTC
            checkin = fields.Datetime.to_datetime("2027-06-11 14:00:00")
            not_due = self._reservation(
                self.room2, checkin=checkin, checkout=checkin + timedelta(days=1)
            )
            (due | not_due).confirmed_reservation()
            self.env["hotel.reservation"].reservation_reminder_24hrs()
        self.assertEqual(self._reminders(due | not_due).mapped("res_id"), due.ids)

    # Room status cron

    def test_cron_room_line(self):
        self.room.write({"isroom": False})
        self.assertEqual(self.room.status, "occupied")
        self.env["hotel.room"].cron_room_line()
        self.assertEqual(self.room.status, "available")

    # Quick reservation and summary

    def test_quick_reservation(self):
        wizard_obj = self.env["quick.room.reservation"].with_context(
            tz="UTC",
            default_room_id=self.room.id,
            quick_reservation_date=fields.Date.to_string(self.base.date()),
        )
        defaults = wizard_obj.default_get(["check_in", "check_out", "room_id"])
        self.assertEqual(defaults["check_in"], self.base)
        self.assertEqual(defaults["check_out"], self.base + timedelta(days=1))
        wizard = wizard_obj.create({"partner_id": self.partner.id})
        wizard._onchange_partner_id_res()
        action = wizard.room_reserve()
        reservation = self.env["hotel.reservation"].browse(action["res_id"])
        self.assertEqual(action["res_model"], "hotel.reservation")
        self.assertEqual(reservation.reservation_line.reserve, self.room)
        self.assertEqual(reservation.reservation_line.categ_id, self.room_type)
        self.assertEqual(reservation.adults, 1)

    def test_summary(self):
        reservation = self._reservation()
        reservation.confirmed_reservation()
        in_folio = self._reservation(self.room2)
        in_folio.confirmed_reservation()
        in_folio.create_folio()
        summary = (
            self.env["room.reservation.summary"]
            .with_user(self.hotel_user)
            .with_context(tz="UTC")
            .create(
                {
                    "date_from": self.base.date() - timedelta(days=1),
                    "date_to": self.base.date() + timedelta(days=3),
                }
            )
        )
        data = summary.summary_data
        self.assertEqual(len(data["days"]), 5)
        rows = {row["id"]: row["cells"] for row in data["rooms"]}
        states = [cell["state"] for cell in rows[self.room.id]]
        # two nights: arrival day and the next one, the departure day is free
        self.assertEqual(states, ["free", "reserved", "reserved", "free", "free"])
        self.assertEqual(rows[self.room.id][1]["res_id"], reservation.id)
        self.assertEqual(rows[self.room2.id][1]["state"], "folio")
        self.assertEqual(rows[self.room2.id][1]["res_id"], in_folio.folio_id.id)
        with self.assertRaises(ValidationError):
            summary.date_to = summary.date_from - timedelta(days=1)

    def test_summary_period_capped(self):
        summary = self.env["room.reservation.summary"].create(
            {
                "date_from": self.base.date(),
                "date_to": self.base.date() + timedelta(days=400),
            }
        )
        self.assertEqual(len(summary.summary_data["days"]), 93)

    # Reports

    def _render(self, method, date_start, date_end):
        wizard = self.env["hotel.reservation.wizard"].create(
            {"date_start": date_start, "date_end": date_end}
        )
        action = getattr(wizard, method)()
        return (
            self.env["ir.actions.report"]
            ._render_qweb_html(action["report_name"], wizard.ids, data=action["data"])[
                0
            ]
            .decode()
        )

    def test_reports(self):
        reservation = self._reservation(lines=[self.room, self.room2])
        reservation.confirmed_reservation()
        start, end = self.base - timedelta(days=1), self.base + timedelta(days=5)
        for method in (
            "report_checkin_detail",
            "report_checkout_detail",
            "report_reservation_detail",
        ):
            html = self._render(method, start, end)
            self.assertIn(reservation.reservation_no, html, method)
            self.assertIn("Test Single-102", html, method)
            self.assertIn(str(start.year), html.split("To:")[0], method)
            self.assertIn("<h2", html, method)
        html = self._render("report_maxroom_detail", start, end)
        self.assertIn("Test Single-101", html)

    def test_report_period(self):
        reservation = self._reservation()
        reservation.confirmed_reservation()
        later = self.base + timedelta(days=30)
        html = self._render("report_checkin_detail", later, later + timedelta(days=5))
        self.assertNotIn(reservation.reservation_no, html)
        self.assertIn("No reservation in this period.", html)

    def test_report_room_usage(self):
        for start in (0, 3):
            self._reservation(start=start).confirmed_reservation()
        self._reservation(self.room2).confirmed_reservation()
        usage = self.env["report.hotel_reservation.report_maxroom_qweb"]
        detail = usage._get_room_used_detail(
            self.base - timedelta(days=1), self.base + timedelta(days=10)
        )
        self.assertEqual(
            detail,
            [
                {"name": "Test Single-101", "no_of_times_used": 2},
                {"name": "Test Single-102", "no_of_times_used": 1},
            ],
        )
