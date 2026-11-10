# Copyright (C) 2024-TODAY Serpent Consulting Services Pvt. Ltd. (<http://www.serpentcs.com>).
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import models


class HotelFolioLine(models.Model):
    _inherit = "hotel.folio.line"

    def _get_room_holder(self, room):
        """A confirmed reservation holds its rooms too, except against the
        folio created from it."""
        holder = super()._get_room_holder(room)
        if holder:
            return holder
        booking = (
            self.env["hotel.room.reservation.line"]
            .sudo()
            .search(
                [
                    ("room_id", "=", room.id),
                    ("status", "=", "confirm"),
                    ("reservation_id", "!=", self.folio_id.reservation_id.id),
                    ("check_in", "<", self.checkout_date),
                    ("check_out", ">", self.checkin_date),
                ],
                limit=1,
            )
        )
        if booking:
            return self.env._("reservation %s", booking.reservation_id.reservation_no)
        return False

    def write(self, vals):
        """
        Overrides ORM write method to update Hotel Room Reservation line history.
        @param vals: dictionary of field values.
        """
        if {"product_id", "checkin_date", "checkout_date"} & set(vals):
            reservation_line_obj = self.env["hotel.room.reservation.line"]
            room_obj = self.env["hotel.room"]
            for line in self.filtered(
                lambda line: (
                    line.is_reserved
                    and line.folio_id.reservation_id
                    and line.product_id
                    and line.checkin_date
                    and line.checkout_date
                )
            ):
                old_room = room_obj.search(
                    [("product_id", "=", line.product_id.id)], limit=1
                )
                new_room = room_obj.search(
                    [("product_id", "=", vals.get("product_id", line.product_id.id))],
                    limit=1,
                )
                if old_room and new_room:
                    # by reservation and room: the dates of the booking may
                    # have followed the stay dates of the folio
                    reservation_line_obj.search(
                        [
                            ("reservation_id", "=", line.folio_id.reservation_id.id),
                            ("room_id", "=", old_room.id),
                        ]
                    ).write(
                        {
                            "room_id": new_room.id,
                            "check_in": vals.get("checkin_date", line.checkin_date),
                            "check_out": vals.get("checkout_date", line.checkout_date),
                        }
                    )
        return super().write(vals)
