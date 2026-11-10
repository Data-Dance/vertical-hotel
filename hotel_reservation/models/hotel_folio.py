# Copyright (C) 2024-TODAY Serpent Consulting Services Pvt. Ltd. (<http://www.serpentcs.com>).
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import fields, models


class HotelFolio(models.Model):
    _inherit = "hotel.folio"
    _order = "reservation_id desc"

    reservation_id = fields.Many2one(
        "hotel.reservation", ondelete="restrict", index=True
    )

    def action_cancel(self):
        """Cancel the reservation of the folios as well: its room bookings
        would keep the rooms of the cancelled stay reserved for good."""
        res = super().action_cancel()
        reservations = self.filtered(
            lambda folio: folio.state == "cancel"
        ).reservation_id.filtered(lambda reservation: reservation.state == "done")
        # not while another folio of the reservation still stands
        reservations -= self.search(
            [("reservation_id", "in", reservations.ids), ("state", "!=", "cancel")]
        ).reservation_id
        if reservations:
            reservations.state = "cancel"
            self.env["hotel.room.reservation.line"].search(
                [("reservation_id", "in", reservations.ids)]
            ).unlink()
            reservations.reservation_line.reserve._update_room_status()
        return res

    def write(self, vals):
        res = super().write(vals)
        if "checkin_date" in vals or "checkout_date" in vals:
            # keep the room bookings of the reservation on the stay dates
            for folio in self.filtered("reservation_id"):
                self.env["hotel.room.reservation.line"].search(
                    [("reservation_id", "=", folio.reservation_id.id)]
                ).write(
                    {
                        "check_in": folio.checkin_date,
                        "check_out": folio.checkout_date,
                    }
                )
        return res
