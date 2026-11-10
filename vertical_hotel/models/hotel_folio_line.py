# Copyright (C) 2024-TODAY Serpent Consulting Services Pvt. Ltd. (<http://www.serpentcs.com>).
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).


from datetime import UTC
from typing import ClassVar
from zoneinfo import ZoneInfo

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class HotelFolioLine(models.Model):
    _name = "hotel.folio.line"
    _description = "Hotel Folio Line"
    _inherits: ClassVar[dict] = {"sale.order.line": "order_line_id"}

    order_line_id = fields.Many2one(
        "sale.order.line",
        required=True,
        ondelete="cascade",
    )
    folio_id = fields.Many2one("hotel.folio", ondelete="cascade")
    checkin_date = fields.Datetime("Check In", required=True)
    checkout_date = fields.Datetime("Check Out", required=True)
    is_reserved = fields.Boolean(help="True when folio line created from Reservation")

    def _update_folio_line(self, records):
        """
        Sync folio room lines with hotel rooms.
        Works with:
          - hotel.folio   → updates all its room_line_ids
          - hotel.folio.line → updates only given lines
        """
        FolioRoomLine = self.env["folio.room.line"]
        hotel_room_obj = self.env["hotel.room"]

        product_ids = records.mapped("product_id").ids
        rooms = hotel_room_obj.search([("product_id", "in", product_ids)])
        room_by_product = {room.product_id.id: room for room in rooms}

        folio_ids = records.mapped("folio_id").ids
        existing_lines = FolioRoomLine.search(
            [("folio_id", "in", folio_ids), ("room_id", "in", rooms.ids)]
        )
        existing_map = {
            (line.folio_id.id, line.room_id.id): line for line in existing_lines
        }

        rooms_to_update = self.env["hotel.room"]
        vals_list = []
        for record in records:
            room = room_by_product.get(record.product_id.id)
            if not room:
                continue

            if (record.folio_id.id, room.id) not in existing_map:
                rooms_to_update |= room
                vals_list.append(
                    {
                        "room_id": room.id,
                        "check_in": record.checkin_date,
                        "check_out": record.checkout_date,
                        "folio_id": record.folio_id.id,
                    }
                )

        if rooms_to_update:
            rooms_to_update.write({"isroom": False})
        if vals_list:
            FolioRoomLine.create(vals_list)

    @api.model_create_multi
    def create(self, vals_list):
        """
        Override ORM create method to auto-link order_id
        from folio when folio_id is provided.
        """
        Folio = self.env["hotel.folio"]
        for vals in vals_list:
            folio_id = vals.get("folio_id")
            folio = Folio
            if folio_id:
                folio = Folio.browse(folio_id).exists()
                if folio:
                    vals["order_id"] = folio.order_id.id
            # The form sets the quantity through the onchange on the dates;
            # do the same for a line created without it and without quantity.
            if "product_uom_qty" not in vals:
                nights = folio.warehouse_id.company_id._get_hotel_stay_nights(
                    fields.Datetime.to_datetime(vals.get("checkin_date")),
                    fields.Datetime.to_datetime(vals.get("checkout_date")),
                )
                if nights:
                    vals["product_uom_qty"] = nights

        lines = super().create(vals_list)
        for line in lines:
            line._update_folio_line(line)
        return lines

    def write(self, vals):
        """Keep the room occupancy on the room and the dates of the lines."""
        if self.env.context.get("hotel_v19_import") or not (
            {"product_id", "checkin_date", "checkout_date"} & set(vals)
        ):
            return super().write(vals)
        old_stays = [
            (line, line.product_id, line.checkin_date, line.checkout_date)
            for line in self
        ]
        res = super().write(vals)
        for line, product, checkin, checkout in old_stays:
            line._move_folio_room_line(product, checkin, checkout)
        if (
            "checkin_date" in vals or "checkout_date" in vals
        ) and "product_uom_qty" not in vals:
            for line in self:
                nights = line._get_stay_nights()
                if line.product_uom_qty != nights:
                    line.product_uom_qty = nights
        return res

    def _move_folio_room_line(self, old_product, old_checkin, old_checkout):
        """Move the occupancy written for the previous room and dates of
        this line to its current ones."""
        self.ensure_one()
        hotel_room_obj = self.env["hotel.room"]
        FolioRoomLine = self.env["folio.room.line"]
        old_room = hotel_room_obj.search([("product_id", "=", old_product.id)], limit=1)
        new_room = hotel_room_obj.search(
            [("product_id", "=", self.product_id.id)], limit=1
        )
        domain = [("folio_id", "=", self.folio_id.id), ("room_id", "=", old_room.id)]
        occupancy = old_room and FolioRoomLine.search(
            domain + [("check_in", "=", old_checkin), ("check_out", "=", old_checkout)],
            limit=1,
        )
        if old_room and not occupancy:
            # dates edited before occupancy followed the line: the occupancy
            # of that room is this line's if no other line has the room
            same_room = self.folio_id.room_line_ids.filtered(
                lambda line: line != self and line.product_id == old_product
            )
            occupancy = not same_room and FolioRoomLine.search(domain, limit=1)
        if not occupancy:
            self._update_folio_line(self)
        elif not new_room:
            occupancy.unlink()
        else:
            occupancy.write(
                {
                    "room_id": new_room.id,
                    "check_in": self.checkin_date,
                    "check_out": self.checkout_date,
                }
            )
        if old_room != new_room:
            old_room.write({"isroom": True})
            new_room.write({"isroom": False})

    @api.constrains("product_id", "checkin_date", "checkout_date")
    def _check_folio_double_booking(self):
        if self.env.context.get("hotel_v19_import"):
            return
        self.folio_id._check_duplicate_folio_room_line()
        self._check_room_availability()

    def _get_room_holder(self, room):
        """Name of another stay holding ``room`` during the stay of this
        line, if there is one. A stay ending when this one starts, or
        starting when it ends, does not hold the room."""
        self.ensure_one()
        other_line = self.sudo().search(
            [
                ("product_id", "=", room.product_id.id),
                ("folio_id", "!=", False),
                ("folio_id", "!=", self.folio_id.id),
                ("folio_id.state", "!=", "cancel"),
                ("checkin_date", "<", self.checkout_date),
                ("checkout_date", ">", self.checkin_date),
            ],
            limit=1,
        )
        if other_line:
            return self.env._("folio %s", other_line.folio_id.display_name)
        return False

    def _check_room_availability(self):
        """Refuse a room that another folio holds for an overlapping period.

        The rooms are locked first: two transactions booking the same room
        cannot both find it free."""
        lines = self.filtered(
            lambda line: (
                line.folio_id
                and line.folio_id.state != "cancel"
                and line.product_id
                and line.checkin_date
                and line.checkout_date
            )
        )
        rooms = (
            self.env["hotel.room"]
            .sudo()
            .search([("product_id", "in", lines.product_id.ids)], order="id")
        )
        rooms._lock_for_booking()
        room_by_product = {room.product_id: room for room in rooms}
        for line in lines:
            room = room_by_product.get(line.product_id)
            holder = room and line._get_room_holder(room)
            if holder:
                raise ValidationError(
                    self.env._(
                        "Room %(room)s is not available: %(holder)s holds it "
                        "for an overlapping period.",
                        room=room.name,
                        holder=holder,
                    )
                )

    @api.constrains("checkin_date", "checkout_date")
    def _check_dates(self):
        """
        This method is used to validate the checkin_date and checkout_date.
        -------------------------------------------------------------------
        @param self: object pointer
        @return: raise warning depending on the validation
        """
        if self.env.context.get("hotel_v19_import"):
            return
        for rec in self:
            if rec.checkin_date >= rec.checkout_date:
                raise ValidationError(
                    rec.env._(
                        """Room line Check In Date Should be """
                        """less than the Check Out Date!"""
                    )
                )

    @api.constrains("checkin_date")
    def _check_checkin_not_before_folio(self):
        """A stay does not start before the day of its folio, unless a hotel
        manager says so (back-dated correction). Days are those of the hotel,
        not of UTC, and only a new check-in is checked: the rest of a
        back-dated line stays editable."""
        if self.env.context.get("hotel_v19_import") or self.env.user.has_group(
            "vertical_hotel.group_hotel_manager"
        ):
            return
        for rec in self:
            if not (rec.folio_id.date_order and rec.checkin_date):
                continue
            folio = rec.folio_id
            hotel_tz = ZoneInfo(
                folio.company_id.partner_id.tz or folio.create_uid.tz or "UTC"
            )
            checkin, date_order = (
                value.replace(tzinfo=UTC).astimezone(hotel_tz).date()
                for value in (rec.checkin_date, rec.folio_id.date_order)
            )
            if checkin < date_order:
                raise ValidationError(
                    rec.env._(
                        """Room line check in date should be """
                        """greater than the current date."""
                    )
                )

    def unlink(self):
        """
        Overrides orm unlink method.
        @param self: The object pointer
        @return: True/False.
        """

        hotel_room_obj = self.env["hotel.room"]
        hotel_room_line_obj = self.env["folio.room.line"]

        order_lines = self.mapped("order_line_id")
        for line in self:
            if line.product_id:
                rooms = hotel_room_obj.search(
                    [("product_id", "=", line.order_line_id.product_id.id)]
                )

                folio_room_lines = hotel_room_line_obj.search(
                    [
                        ("folio_id", "=", line.folio_id.id),
                        ("room_id", "in", rooms.ids),
                    ]
                )

                if folio_room_lines:
                    folio_room_lines.unlink()

                if rooms:
                    rooms.write({"isroom": True, "status": "available"})

        res = super().unlink()

        if order_lines:
            order_lines.unlink()
        return res

    # # Migrated _onchange_product_id method v15 to v18.
    @api.onchange("product_id")
    def _onchange_product_id_warning(self):
        self.ensure_one()
        if not self.product_id or not self.order_line_id:
            return
        product = self.product_id
        company = self.company_id or self.order_id.company_id or self.env.company
        taxes = product.taxes_id._filter_taxes_by_company(company)
        self.update(
            {
                "name": product.name,
                "product_uom_id": product.uom_id,
                "product_uom_qty": self.product_uom_qty,
                "price_unit": product.list_price,
                "tax_ids": self.order_id.fiscal_position_id.map_tax(taxes),
            }
        )
        if self.folio_id:
            # the price of the folio's pricelist rather than the list price
            self.update(
                self.folio_id._get_line_price_values(
                    product, self.product_uom_qty, product.uom_id
                )
            )
        if product.sale_line_warn_msg:
            return {
                "warning": {
                    "title": self.env._("Warning for %s", product.name),
                    "message": product.sale_line_warn_msg,
                }
            }

    @api.onchange("checkin_date", "checkout_date")
    def _onchange_checkin_checkout_dates(self):
        """
        When you change checkin_date or checkout_date it will check it
        and update the quantity of hotel folio line
        -----------------------------------------------------------------
        @param self: object pointer
        """

        self.product_uom_qty = self._get_stay_nights()

    def _get_stay_nights(self):
        self.ensure_one()
        company = self.folio_id.warehouse_id.company_id
        return company._get_hotel_stay_nights(self.checkin_date, self.checkout_date)

    def copy_data(self, default=None):
        """
        @param self: object pointer
        @param default: dict of default values to be set
        """

        sale_line_obj = self.order_line_id
        return sale_line_obj.copy_data(default=default)
